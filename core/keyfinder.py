# -*- coding: utf-8 -*-
"""墨溯 · 微信数据库密钥提取器（只读内存扫描，零第三方依赖）。

## 它解决什么问题

墨溯要能在一台**全新电脑**上拿数据库密钥：用户装了微信、登录着，墨溯就能取到密钥，
不需要用户懂内存、不需要外部工具、不需要注入或 hook。

## 原理（本机 4.1.15.13 实测验证，raw XOR mask == 已知密钥，逐字节一致）

1. 微信把数据库口令（32 字节 passphrase）交给 WCDB 后，WCDB 会把它
   **用一段固定掩码 XOR 之后**留在进程堆上。所以直接搜明文口令永远搜不到——
   这是很多工具在 4.1 上失效的原因。
2. 那段掩码不是随机的：它是 `Weixin.dll` 代码段里的一段常量，
   以 4 条 `mov rdx, imm64` + `test rax, rax` 的指令序列出现，可以**静态提取**。
3. 口令所在对象在内存里有稳定结构（6 字节指针 + 补零 + 长度 32 的字段），
   按结构定位再解指针即可拿到 XOR 后的候选。
4. 候选 XOR 掩码还原后，用目标库第一页做 **PBKDF2 + HMAC-SHA512 校验**
   （SQLCipher 4 的权威判据），通过即确认。

## 安全边界

- 只用 `ReadProcessMemory` **只读**读取登录态内存：不写内存、不注入 DLL、
  不挂调试器、不改微信任何文件、不联网。
- 取不到就明确报错并告诉用户下一步，绝不"猜一个看起来像的"。

## 平台

目前仅 Windows（微信 PC 4.x）。其他平台返回 PLATFORM_UNSUPPORTED。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac
import os
import re
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400

MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01

TH32CS_SNAPPROCESS = 0x00000002

# 口令对象在内存里的结构锚点：指针(6B) + 00×10 + size=32 + 尾部标记
# 前 6 字节是指针低位，随地址变化，所以只拿后面的常量段做锚
ANCHOR = b"\x00" * 10 + b"\x20" + b"\x00" * 7 + b"\x2f" + b"\x00" * 7
ANCHOR_PTR_BYTES = 6

# 掩码提取签名：mov rdx, imm64 (48 BA ..) ×4，收尾 test rax,rax (48 85 C0)
MASK_PATTERN_STRICT = re.compile(
    b"^\x48\xBA(.{8})"
    b".{3,8}?\x48\xBA(.{8})"
    b".{3,8}?\x48\xBA(.{8})"
    b".{3,8}?\x48\xBA(.{8})"
    b".{3,8}?\x48\x85\xC0",
    re.DOTALL)
# 宽松兜底：版本变化时收尾指令可能不同，放宽到只要 4 个连续 imm64
MASK_PATTERN_LOOSE = re.compile(
    b"^\x48\xBA(.{8})"
    b".{3,12}?\x48\xBA(.{8})"
    b".{3,12}?\x48\xBA(.{8})"
    b".{3,12}?\x48\xBA(.{8})",
    re.DOTALL)

PAGE_SIZE = 4096
SALT_SIZE = 16
HMAC_SIZE = 64
IV_SIZE = 16
KDF_ITER = 256000
KEY_SIZE = 32

IMAGE_SCN_MEM_EXECUTE = 0x20000000

ProgressCb = Callable[[str, int, int, str], None]


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------

@dataclass
class KeyResult:
    ok: bool
    key_hex: str = ""
    stage: str = ""                 # 失败/中断时停在哪一步
    error: str = ""
    hint: str = ""
    pid: int = 0
    addr: int = 0
    raw_hex: str = ""
    mask_hex: str = ""
    dll: str = ""
    scanned_bytes: int = 0
    candidates: int = 0
    tried: int = 0
    elapsed: float = 0.0
    diagnostics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "keyHex": self.key_hex, "stage": self.stage,
            "error": self.error, "hint": self.hint, "pid": self.pid,
            "addr": hex(self.addr) if self.addr else "",
            "rawHex": self.raw_hex, "maskHex": self.mask_hex, "dll": self.dll,
            "scannedBytes": self.scanned_bytes, "candidates": self.candidates,
            "tried": self.tried, "elapsed": round(self.elapsed, 2),
            "diagnostics": self.diagnostics,
        }


class KeyFinderError(Exception):
    """带用户可读提示的失败。"""

    def __init__(self, stage: str, error: str, hint: str = ""):
        super().__init__(error)
        self.stage = stage
        self.error = error
        self.hint = hint


# ---------------------------------------------------------------------------
# Windows 进程/内存 基元
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)

    k32.OpenProcess.restype = wt.HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.ReadProcessMemory.restype = wt.BOOL
    k32.ReadProcessMemory.argtypes = [wt.HANDLE, wt.LPCVOID, wt.LPVOID,
                                      ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    k32.CloseHandle.restype = wt.BOOL
    k32.CloseHandle.argtypes = [wt.HANDLE]

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * 260),
        ]

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    class MEMORY_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_void_p),
            ("AllocationBase", ctypes.c_void_p),
            ("AllocationProtect", wt.DWORD),
            ("PartitionId", wt.WORD),
            ("RegionSize", ctypes.c_size_t),
            ("State", wt.DWORD),
            ("Protect", wt.DWORD),
            ("Type", wt.DWORD),
        ]

    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    k32.Process32FirstW.restype = wt.BOOL
    k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.restype = wt.BOOL
    k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.VirtualQueryEx.restype = ctypes.c_size_t
    k32.VirtualQueryEx.argtypes = [wt.HANDLE, wt.LPCVOID, ctypes.c_void_p, ctypes.c_size_t]
    psapi.GetProcessMemoryInfo.restype = wt.BOOL
    psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD]
    psapi.EnumProcessModules.restype = wt.BOOL
    psapi.EnumProcessModules.argtypes = [wt.HANDLE, ctypes.POINTER(wt.HMODULE),
                                         wt.DWORD, ctypes.POINTER(wt.DWORD)]
    psapi.GetModuleFileNameExW.restype = wt.DWORD
    psapi.GetModuleFileNameExW.argtypes = [wt.HANDLE, wt.HMODULE,
                                           ctypes.c_wchar_p, wt.DWORD]
else:  # pragma: no cover - 非 Windows 只提供占位，调用方会得到明确错误
    k32 = None
    psapi = None


def _require_windows() -> None:
    if sys.platform != "win32":
        raise KeyFinderError(
            "platform", "密钥自动提取目前仅支持 Windows（微信 PC 4.x）",
            "其他平台请用 `mosu key set <64位hex>` 手动填入密钥。")


def list_processes() -> list[tuple[int, str, int]]:
    """枚举进程 [(pid, 镜像名, 工作集字节)]。按内存降序，主进程排前面。"""
    _require_windows()
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == ctypes.c_void_p(-1).value or not snap:
        raise KeyFinderError("process", "无法枚举进程（CreateToolhelp32Snapshot 失败）")
    out = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            pid = entry.th32ProcessID
            mem = 0
            h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
            if h:
                try:
                    pmc = PROCESS_MEMORY_COUNTERS()
                    pmc.cb = ctypes.sizeof(pmc)
                    if psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                        mem = pmc.WorkingSetSize
                finally:
                    k32.CloseHandle(h)
            out.append((pid, entry.szExeFile, mem))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    out.sort(key=lambda x: -x[2])
    return out


def find_weixin_pids(min_mem: int = 80 * 1024 * 1024) -> list[tuple[int, int]]:
    """微信主进程候选 [(pid, mem)]，按内存降序。"""
    procs = list_processes()
    return [(pid, mem) for pid, name, mem in procs
            if name.lower() in ("weixin.exe", "wechat.exe") and mem >= min_mem]


def main_module_dir(pid: int) -> Path | None:
    """进程主模块所在目录（微信安装目录下的版本子目录）。"""
    _require_windows()
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return None
    try:
        mods = (wt.HMODULE * 1)()
        need = wt.DWORD(0)
        if not psapi.EnumProcessModules(h, mods, ctypes.sizeof(mods), ctypes.byref(need)):
            return None
        buf = ctypes.create_unicode_buffer(4096)
        n = psapi.GetModuleFileNameExW(h, mods[0], buf, 4096)
        if not n:
            return None
        return Path(buf.value).parent
    finally:
        k32.CloseHandle(h)


def find_weixin_dll(install_dir: Path | None = None) -> Path | None:
    """定位 Weixin.dll（微信主逻辑与掩码常量所在模块）。"""
    if install_dir:
        for p in [install_dir / "Weixin.dll", *(install_dir.glob("*/Weixin.dll"))]:
            if p.exists():
                return p
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", "")):
        if not base:
            continue
        root = Path(base) / "Tencent" / "Weixin"
        if root.exists():
            hits = sorted(root.glob("**/Weixin.dll"),
                          key=lambda p: -p.stat().st_size)
            if hits:
                return hits[0]
    return None


def read_process_bytes(pid: int, addr: int, size: int) -> bytes | None:
    """读目标进程任意地址的字节（只读）。"""
    h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        if k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(got)):
            return buf.raw[: got.value]
    finally:
        k32.CloseHandle(h)
    return None


def iter_regions(pid: int, max_region: int = 512 * 1024 * 1024,
                 progress: ProgressCb | None = None) -> Iterator[tuple[int, int, bytes]]:
    """遍历可读的已提交内存区域，yield (基址, 保护属性, 内容)。"""
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        raise KeyFinderError("memory", f"无法打开进程 {pid}（可能权限不足或已退出）",
                             "请以与微信相同的用户身份运行，不要用管理员身份启动墨溯。")
    try:
        addr = 0
        mbi = MEMORY_BASIC_INFORMATION()
        scanned = 0
        while addr < (1 << 47):
            if not k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                      ctypes.sizeof(mbi)):
                break
            base = mbi.BaseAddress or 0
            size = mbi.RegionSize or 0
            addr = (base + size) if size else addr + 0x1000
            if mbi.State != MEM_COMMIT or mbi.Protect in (PAGE_NOACCESS, 0):
                continue
            if size == 0 or size > max_region:
                continue
            buf = ctypes.create_string_buffer(size)
            got = ctypes.c_size_t(0)
            if not k32.ReadProcessMemory(h, ctypes.c_void_p(base), buf, size,
                                         ctypes.byref(got)) or got.value == 0:
                continue
            scanned += got.value
            if progress:
                progress("scan", scanned, 0, f"已扫描 {scanned / 1e6:.0f} MB")
            yield base, mbi.Protect, buf.raw[: got.value]
    finally:
        k32.CloseHandle(h)


# ---------------------------------------------------------------------------
# PE 解析 + 掩码静态提取
# ---------------------------------------------------------------------------

def pe_executable_sections(path: Path) -> list[tuple[str, int, int, int]]:
    """零依赖解析 PE，返回可执行节 [(名, 文件偏移, 大小, RVA)]。"""
    data = path.read_bytes()
    if data[:2] != b"MZ":
        return []
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
        return []
    coff = e_lfanew + 4
    nsec, = struct.unpack_from("<H", data, coff + 2)
    size_opt, = struct.unpack_from("<H", data, coff + 16)
    sec_off = coff + 20 + size_opt
    out = []
    for i in range(nsec):
        b = sec_off + i * 40
        if b + 40 > len(data):
            break
        name = data[b:b + 8].rstrip(b"\x00").decode("ascii", "replace")
        _vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, b + 8)
        chars, = struct.unpack_from("<I", data, b + 36)
        if chars & IMAGE_SCN_MEM_EXECUTE and rawsize:
            out.append((name, rawptr, rawsize, vaddr))
    return out


def extract_xor_masks(dll: Path, loose: bool = True) -> list[bytes]:
    """从 dll 代码段静态提取候选 XOR 掩码（32 字节）。

    严格签名优先（命中即高置信），宽松签名兜底（版本变化时的保险）。
    """
    if not dll or not dll.exists():
        return []
    data = dll.read_bytes()
    secs = pe_executable_sections(dll)
    blobs = [(name, data[off:off + size], vaddr) for name, off, size, vaddr in secs]
    if not blobs:
        blobs = [("?", data, 0)]

    out: list[bytes] = []
    seen: set[bytes] = set()

    def collect(pattern: re.Pattern, window: int) -> None:
        for _name, blob, _vaddr in blobs:
            i, n = 0, len(blob)
            while True:
                idx = blob.find(b"\x48\xBA", i)
                if idx == -1 or idx + window > n:
                    break
                m = pattern.match(blob[idx:idx + window])
                if m:
                    kb = b"".join(m.groups())
                    if kb not in seen:
                        seen.add(kb)
                        out.append(kb)
                    i = idx + len(m.group(0))
                else:
                    i = idx + 1

    collect(MASK_PATTERN_STRICT, 85)
    if not out and loose:
        collect(MASK_PATTERN_LOOSE, 90)
    return out


# ---------------------------------------------------------------------------
# 候选扫描
# ---------------------------------------------------------------------------

def scan_raw_keys(pid: int, progress: ProgressCb | None = None,
                  limit: int = 20000,
                  cancel: threading.Event | None = None) -> list[tuple[int, bytes]]:
    """按结构锚点扫描进程内存，返回 [(地址, 32字节候选)]。

    只读扫描，不写不注入。锚点命中处向前 6 字节是数据指针，解引用取 32 字节。
    """
    addrs: list[int] = []
    for base, _prot, data in iter_regions(pid, progress=progress):
        p = data.find(ANCHOR)
        while p != -1:
            s = p - ANCHOR_PTR_BYTES
            if s >= 0:
                try:
                    a = struct.unpack_from("<Q", data, s)[0]
                except struct.error:
                    a = 0
                if 0x10000 < a < (1 << 48):
                    addrs.append(a)
            p = data.find(ANCHOR, p + 1)
        if len(addrs) >= limit or (cancel and cancel.is_set()):
            break
    if cancel and cancel.is_set():
        return []

    # 去重保序，再逐个解引用
    uniq: list[int] = []
    seen: set[int] = set()
    for a in addrs:
        if a not in seen:
            seen.add(a)
            uniq.append(a)

    raws: list[tuple[int, bytes]] = []
    total = len(uniq)
    for i, a in enumerate(uniq):
        if progress and i % 64 == 0:
            progress("deref", i, total, f"解析候选 {i}/{total}")
        raw = read_process_bytes(pid, a, KEY_SIZE)
        if raw and len(raw) == KEY_SIZE and raw != b"\x00" * KEY_SIZE:
            raws.append((a, raw))
    return raws


# ---------------------------------------------------------------------------
# 校验（SQLCipher 4：PBKDF2 + HMAC-SHA512）
# ---------------------------------------------------------------------------

def _derive_and_check(cand: bytes, page1: bytes) -> bool:
    """候选口令是否与该库第一页匹配。纯标准库实现，与 core/sqlcipher4 同一判据。"""
    salt = page1[:SALT_SIZE]
    body = page1[SALT_SIZE:PAGE_SIZE - HMAC_SIZE]
    stored_mac = page1[PAGE_SIZE - HMAC_SIZE:PAGE_SIZE]
    enc_key = hashlib.pbkdf2_hmac("sha512", cand, salt, KDF_ITER, KEY_SIZE)
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, KEY_SIZE)
    mac = hmac.new(mac_key, body + struct.pack("<I", 1), hashlib.sha512).digest()
    return hmac.compare_digest(mac, stored_mac)


def read_db_first_page(db: Path) -> bytes:
    with open(db, "rb") as f:
        page = f.read(PAGE_SIZE)
    if len(page) != PAGE_SIZE:
        raise KeyFinderError("verify", f"数据库文件不足一页，无法校验：{db}")
    return page


def verify_candidates(cands: list[tuple[bytes, int, bytes | None]], page1: bytes,
                      workers: int = 0, progress: ProgressCb | None = None,
                      cancel: threading.Event | None = None
                      ) -> tuple[bytes, int, bytes | None] | None:
    """并行校验候选，(命中口令, 地址, 掩码) 或 None。

    PBKDF2 256k 单次约 250ms，但 hashlib 会释放 GIL，线程池可真并行。
    """
    if not cands:
        return None
    workers = workers or min(8, (os.cpu_count() or 4))
    total = len(cands)

    def work(item):
        cand, addr, mask = item
        return (cand, addr, mask) if _derive_and_check(cand, page1) else None

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(work, cands):
            done += 1
            if progress and (done % 8 == 0 or done == total):
                progress("verify", done, total, f"校验 {done}/{total}")
            if res:
                return res
            if cancel and cancel.is_set():
                return None
    return None


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------

def find_db_key(db_path: str | Path, pid: int | None = None,
                dll_path: str | Path | None = None,
                workers: int = 0, progress: ProgressCb | None = None,
                cancel: threading.Event | None = None) -> KeyResult:
    """一键取密钥：定位微信 → 提取掩码 → 扫内存 → 校验 → 返回 64 位 hex。"""
    t0 = time.time()
    res = KeyResult(ok=False)
    try:
        _require_windows()
        db = Path(db_path)
        if not db.exists():
            raise KeyFinderError("verify", f"找不到用于校验的数据库：{db}",
                                 "先确认微信数据目录是否正确（含 db_storage 的那一层）。")
        page1 = read_db_first_page(db)

        # 1) 微信进程
        progress and progress("process", 0, 0, "检测微信进程…")
        pids = [(pid, 0)] if pid else find_weixin_pids()
        if not pids:
            raise KeyFinderError(
                "process", "没有检测到运行中的微信主进程",
                "请先在电脑上登录微信（保持运行），再点“自动获取”。")
        res.diagnostics["pids"] = [p for p, _ in pids[:5]]

        # 2) 掩码
        progress and progress("mask", 0, 0, "提取版本掩码…")
        dll = Path(dll_path) if dll_path else None
        if dll is None:
            for cand_pid, _ in pids:
                d = main_module_dir(cand_pid)
                if d:
                    dll = find_weixin_dll(d)
                if dll:
                    break
        if dll is None:
            dll = find_weixin_dll()
        masks: list[bytes] = []
        if dll and dll.exists():
            res.dll = str(dll)
            masks = extract_xor_masks(dll)
        res.diagnostics["masks"] = [m.hex() for m in masks[:8]]
        res.diagnostics["maskCount"] = len(masks)
        # 掩码提取失败不是致命错误：有些版本可能不 XOR，仍要试原始候选
        variants: list[bytes | None] = masks[:8] or []
        variants.append(None)          # None = 不 XOR，直接用原始候选

        # 3) 扫内存（按进程顺序，先主进程）
        raws: list[tuple[int, bytes]] = []
        for cand_pid, _mem in pids[:3]:
            progress and progress("scan", 0, 0, f"扫描进程 {cand_pid} 内存…")
            got = scan_raw_keys(cand_pid, progress=progress, cancel=cancel)
            if got:
                raws = got
                res.pid = cand_pid
                break
        res.candidates = len(raws)
        if not raws:
            raise KeyFinderError(
                "scan", "内存中未找到密钥结构（0 个候选）",
                "常见原因：微信版本太新/太旧导致结构变化；或微信未真正登录。"
                "可尝试重启微信后重试，或在设置里手动填入密钥。")

        # 4) 校验（先试带掩码的，再试原始）
        cands: list[tuple[bytes, int, bytes | None]] = []
        for mask in variants:
            for addr, raw in raws:
                cand = raw if mask is None else bytes(a ^ b for a, b in zip(raw, mask))
                cands.append((cand, addr, mask))
        res.tried = len(cands)
        progress and progress("verify", 0, len(cands), "校验候选密钥…")
        hit = verify_candidates(cands, page1, workers=workers, progress=progress,
                                cancel=cancel)
        if not hit:
            raise KeyFinderError(
                "verify", f"校验了 {len(cands)} 个候选，没有一个能通过 HMAC 校验",
                "密钥可能已更换（微信跨大版本升级会换密钥）。请重启微信后重试；"
                "仍不行就在设置里手动填入密钥。")
        key, addr, mask = hit
        res.ok = True
        res.key_hex = key.hex()
        res.addr = addr
        res.raw_hex = dict(raws).get(addr, b"").hex()
        res.mask_hex = mask.hex() if mask else ""
        res.stage = "done"
    except KeyFinderError as e:
        res.stage = e.stage
        res.error = e.error
        res.hint = e.hint
    except Exception as e:                     # 兜底：任何异常都要变成可读结果
        res.stage = res.stage or "unknown"
        res.error = f"{type(e).__name__}: {e}"
        res.hint = "请重启微信后重试；持续失败可在设置里手动填入密钥。"
    res.elapsed = time.time() - t0
    return res


def self_test(db_path: str | Path) -> dict:
    """环境自检：不取密钥，只报告各环节是否具备条件（供 UI 预检/排障）。"""
    out = {"platform": sys.platform, "ok": False}
    try:
        _require_windows()
        out["platformOk"] = True
        pids = find_weixin_pids()
        out["processes"] = [{"pid": p, "memMB": round(m / 1e6)} for p, m in pids[:5]]
        out["processOk"] = bool(pids)
        dll = None
        for p, _ in pids:
            d = main_module_dir(p)
            if d:
                dll = find_weixin_dll(d)
            if dll:
                break
        if dll is None:
            dll = find_weixin_dll()
        out["dll"] = str(dll) if dll else ""
        out["dllOk"] = bool(dll)
        if dll:
            masks = extract_xor_masks(dll)
            out["maskCount"] = len(masks)
            out["maskOk"] = bool(masks)
        db = Path(db_path)
        out["db"] = str(db)
        out["dbOk"] = db.exists()
        out["ok"] = bool(out.get("processOk") and out.get("dbOk"))
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out
