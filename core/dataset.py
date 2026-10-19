# -*- coding: utf-8 -*-
"""
墨溯 core —— 统一数据层

把「定位账号目录 -> 快照 -> 自有 SQLCipher4 解密 -> WAL 帧合并 -> SQLite 连接」
封装成一个 Dataset 对象，供 CLI / 脚本 / 上层服务复用。

用法:
    from core.dataset import Dataset
    ds = Dataset()
    ds.sessions()[:5]
    ds.messages("张三", limit=20)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Crypto.Cipher import AES  # noqa: E402
from core.sqlcipher4 import (  # noqa: E402
    PAGE_SIZE, RESERVE, DATA_END, IV_OFF, HMAC_OFF, MAGIC,
    derive_enc_key, verify_password,
)
from core.msgparse import parse_message, T_TEXT, T_IMAGE, T_VOICE, T_VIDEO, T_EMOJI, T_APPMSG, T_SYS  # noqa: E402

CACHE = ROOT / ".cache"

# ---- 墨溯自己的密钥库 ----
# 优先级：环境变量 MOSU_HOME > ~/.mosu；历史遗留的旧版密钥库只读兼容
CONFIG_DIR = Path(os.environ.get("MOSU_HOME") or (Path.home() / ".mosu"))
KEYVAULT = CONFIG_DIR / "config.json"
LEGACY_VAULT = Path.home() / ".miyu" / "config.json"   # 历史遗留旧库，只读兼容

# 未配置 dbPath 时扫描的常见位置（微信 Windows 版默认就在 Documents 下）
DEFAULT_ROOTS = [
    Path.home() / "Documents" / "xwechat_files",
    Path.home() / "Documents" / "WeChat Files",
    Path("D:/xwechat_files"),
    Path("C:/xwechat_files"),
]

# 消息类型 -> CLI 上使用的类别名
KIND_BY_TYPE = {
    T_TEXT: "text",
    T_IMAGE: "image",
    T_VOICE: "voice",
    T_VIDEO: "video",
    T_EMOJI: "emoji",
    T_APPMSG: "file",
    T_SYS: "system",
}


# --------------------------------------------------------------------------
# 配置 / 密钥
# --------------------------------------------------------------------------
def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_keyvault() -> dict:
    """读密钥库。新库（~/.mosu）优先；没有则兼容读历史遗留的旧版密钥库。"""
    if KEYVAULT.exists():
        return _read_json(KEYVAULT)
    if LEGACY_VAULT.exists():
        data = _read_json(LEGACY_VAULT)
        if data:
            print(f"[i] 检测到旧密钥库 {LEGACY_VAULT}，跑 `mosu key migrate` "
                  f"迁到 {KEYVAULT} 后即可脱离旧版配置", file=sys.stderr)
            return data
    return {}


def save_keyvault(data: dict) -> Path:
    """写墨溯自己的密钥库（不存在则创建目录）。"""
    KEYVAULT.parent.mkdir(parents=True, exist_ok=True)
    KEYVAULT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return KEYVAULT


def load_key_hex(account: str | None = None) -> str:
    """取 64 位 hex 数据库密钥。

    顺序：环境变量 MOSU_KEY_HEX > 当前账号的 keyHex > 密钥库任意 64 位 hex。
    """
    env = os.environ.get("MOSU_KEY_HEX", "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", env):
        return env.lower()
    _, acc = resolve_account(account)
    k = acc.get("keyHex")
    if isinstance(k, str) and len(k) == 64:
        return k
    data = acc or load_keyvault()

    def walk(o):
        if isinstance(o, str) and len(o) == 64:
            try:
                bytes.fromhex(o)
                return o
            except ValueError:
                return None
        if isinstance(o, dict):
            for v in o.values():
                r = walk(v)
                if r:
                    return r
        if isinstance(o, list):
            for v in o:
                r = walk(v)
                if r:
                    return r
        return None

    k = walk(data)
    if not k:
        raise SystemExit(f"[x] {KEYVAULT} 里没有 64 位 hex 密钥，先跑 `mosu key set <hex>`")
    return k

# --------------------------------------------------------------------------
# 多账号
# --------------------------------------------------------------------------
# 密钥库支持两种形态，读取时自动兼容：
#   旧（单账号扁平）：{"dbPath": ..., "keyHex": ..., "wxid": ...}
#   新（多账号）：    {"current": "wxid_a",
#                     "accounts": {"wxid_a": {"dbPath":..., "keyHex":...}}}
# 首次读取到旧形态会自动升级为新形态（原键保留在账号条目里）。

def _vault_raw() -> dict:
    return _read_json(KEYVAULT)


def _migrate_flat(data: dict) -> tuple[dict, str | None]:
    """旧扁平结构 → 多账号结构。返回 (accounts, current)。"""
    if not data.get("keyHex"):
        return {}, None
    name = data.get("wxid") or "default"
    acc = {k: v for k, v in data.items() if k not in ("accounts", "current")}
    acc.setdefault("wxid", name)
    return {name: acc}, name


def load_accounts() -> tuple[dict, str | None]:
    """读全部账号配置。返回 (accounts, current_name)。旧格式自动升级落盘。"""
    data = _vault_raw()
    if "accounts" in data:
        return data.get("accounts") or {}, data.get("current")
    accounts, current = _migrate_flat(data)
    if accounts:
        save_accounts(accounts, current=current, extra=data)
    return accounts, current


def save_accounts(accounts: dict, current: str | None = None, extra: dict | None = None) -> Path:
    """写回账号配置。默认保留现有文件里的其他顶层键（如 defaultLimit）。"""
    payload = _vault_raw() if extra is None else dict(extra)
    payload.pop("accounts", None)
    payload.pop("current", None)
    # 清掉旧扁平键，避免两份真相
    for k in ("dbPath", "keyHex", "wxid", "imgKeyHex", "imgKeySavedAt"):
        payload.pop(k, None)
    payload["accounts"] = accounts
    if current:
        payload["current"] = current
    KEYVAULT.parent.mkdir(parents=True, exist_ok=True)
    KEYVAULT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return KEYVAULT


def resolve_account(name: str | None = None) -> tuple[str | None, dict]:
    """按优先级选账号：显式 name > MOSU_ACCOUNT > current > 唯一账号 > 空。

    返回 (账号名, 账号配置)。找不到显式指定的账号时抛 SystemExit。
    """
    accounts, current = load_accounts()
    if not accounts:
        return None, {}
    want = (name or "").strip() or os.environ.get("MOSU_ACCOUNT", "").strip() or current
    if want and want in accounts:
        return want, accounts[want]
    if want and want not in accounts:
        raise SystemExit(
            f"[x] 没有名为「{want}」的账号，现有：{', '.join(accounts)}"
            f"（`mosu account list` 查看，`mosu account use <名>` 切换）")
    if len(accounts) == 1:                     # 没指定就用唯一的那个
        only = next(iter(accounts))
        return only, accounts[only]
    raise SystemExit(
        f"[x] 有多个账号但没指定用哪个：{', '.join(accounts)}"
        f"（`mosu account use <名>` 设默认，或加 --wxid <名>）")


def account_field(name: str | None, field: str, default=None):
    """取某个账号的字段（找不到账号时回退到旧扁平读取）。"""
    _, acc = resolve_account(name)
    if acc:
        return acc.get(field, default)
    return load_keyvault().get(field, default)


def find_user_dir(explicit: str | os.PathLike | None = None,
                  account: str | None = None) -> Path:
    """定位微信账号目录（含 db_storage 的那个）。

    顺序：--db-path > 环境变量 MOSU_DB_PATH > 当前账号的 dbPath > 常见默认位置。
    """
    if explicit:
        p = Path(explicit)
        if p.name == "db_storage":
            return p.parent          # 允许直接传 db_storage 目录
        return p
    env = os.environ.get("MOSU_DB_PATH", "").strip()
    if env:
        return Path(env)
    acc_name, acc = resolve_account(account)
    cfg_dir = acc.get("dbPath") if acc else None
    if cfg_dir:
        if Path(cfg_dir).exists():
            return Path(cfg_dir)
        # 账号明确配了目录却不存在 → 报错，绝不静默 fallback 到别的账号数据
        raise SystemExit(f"[x] 账号「{acc_name}」的数据目录不存在：{cfg_dir}")
    if acc:
        # 账号没配 dbPath（可能只配了 key）才允许扫默认位置
        for r in DEFAULT_ROOTS:
            if not r.exists():
                continue
            for d in sorted(r.iterdir()):
                if d.is_dir() and (d / "db_storage").exists():
                    return d
        raise SystemExit(f"[x] 账号「{acc_name}」没配 dbPath，用 --db-path 指定")
    for r in DEFAULT_ROOTS:
        if not r.exists():
            continue
        for d in sorted(r.iterdir()):
            if d.is_dir() and (d / "db_storage").exists():
                return d
    raise SystemExit("[x] 没找到微信数据目录，请用 --db-path 指定")


def parse_time(value) -> int | None:
    """把 '2026-01-01' / '2026-01-01 12:30' / unix 秒 转成时间戳。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    import datetime
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return int(datetime.datetime.strptime(s, fmt).timestamp())
        except ValueError:
            continue
    raise SystemExit(f"[x] 无法解析时间: {value}")


# --------------------------------------------------------------------------
# Dataset
# --------------------------------------------------------------------------
class Dataset:
    """微信本地数据统一入口（解密缓存 + 会话索引 + 查询）。"""

    def __init__(self, data_dir=None, key_hex: str | None = None, refresh=False,
                 quiet=False, with_wal=False, account: str | None = None):
        self.account_name, self.account = resolve_account(account)
        self.user_dir = find_user_dir(data_dir, self.account_name)
        self.db_root = self.user_dir / "db_storage"
        self.key_hex = key_hex or load_key_hex(self.account_name)
        self.key = bytes.fromhex(self.key_hex)
        self.refresh = refresh
        self.quiet = quiet
        self.with_wal = with_wal
        CACHE.mkdir(parents=True, exist_ok=True)

        self._plain: dict[str, Path] = {}
        self._broken: set[str] = set()
        self._con: dict[str, sqlite3.Connection] = {}
        self._contacts: dict[str, dict] | None = None
        self._sessions: list[dict] | None = None
        self._shards: list[str] | None = None
        self._self_ids: dict[str, int] = {}

    # -------------------------------------------------------------- 解密
    def log(self, msg: str) -> None:
        if not self.quiet:
            print(msg, file=sys.stderr)

    def _enc_key(self, snap: Path) -> bytes:
        with open(snap, "rb") as f:
            salt = f.read(16)
        return derive_enc_key(self.key, salt)

    def _decrypt_page(self, enc_key: bytes, page: bytes, pgno: int) -> bytes:
        iv = page[IV_OFF:HMAC_OFF]
        if pgno == 1:
            pt = MAGIC + AES.new(enc_key, AES.MODE_CBC, iv).decrypt(page[16:DATA_END])
        else:
            pt = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(page[:DATA_END])
        return pt + b"\x00" * RESERVE

    def _decrypt_main(self, snap: Path, dst: Path) -> None:
        enc_key = self._enc_key(snap)
        size = snap.stat().st_size
        npages = size // PAGE_SIZE
        with open(snap, "rb") as fin, open(dst, "wb") as fout:
            for i in range(npages):
                pgno = i + 1
                fout.write(self._decrypt_page(enc_key, fin.read(PAGE_SIZE), pgno))
        # 尾部不足一页（异常）直接丢弃

    def _apply_wal(self, snap: Path, wal_snap: Path, plain: Path) -> int:
        """解密 WAL 帧并把页合并进明文库。返回合并帧数。"""
        enc_key = self._enc_key(snap)
        raw = wal_snap.read_bytes()
        if len(raw) < 32:
            return 0
        frame = 24 + PAGE_SIZE
        pages: dict[int, bytes] = {}
        ntrunc = 0
        i = 32
        while i + frame <= len(raw):
            fh = raw[i:i + 24]
            pgno = struct.unpack(">I", fh[:4])[0]
            nt = struct.unpack(">I", fh[4:8])[0]
            if pgno:
                pages[pgno] = self._decrypt_page(enc_key, raw[i + 24:i + frame], pgno)
                if nt:
                    ntrunc = nt
            i += frame
        if not pages:
            return 0
        size = plain.stat().st_size
        with open(plain, "r+b") as f:
            for pgno, data in sorted(pages.items()):
                off = (pgno - 1) * PAGE_SIZE
                if off > size:          # 中间缺页，放弃（避免损坏）
                    return 0
                f.seek(off)
                f.write(data)
            if ntrunc and ntrunc * PAGE_SIZE < size:
                f.truncate(ntrunc * PAGE_SIZE)
        return len(pages)

    def _integrity_ok(self, plain: Path) -> bool:
        try:
            con = sqlite3.connect(str(plain))
            r = con.execute("PRAGMA quick_check").fetchone()
            con.close()
            return bool(r) and r[0] == "ok"
        except Exception:
            return False

    def plain(self, rel: str) -> Path | None:
        """拿到某个库的明文副本（带缓存）。失败返回 None（微信正在写入导致快照损坏时）。"""
        if rel in self._plain:
            return self._plain[rel]
        src = self.db_root / rel
        if not src.exists():
            raise SystemExit(f"[x] 找不到数据库 {src}")
        name = src.stem
        snap = CACHE / f"{name}_snap.db"
        plain = CACHE / f"{name}_plain.db"
        wal_snap = CACHE / f"{name}_snap.db-wal"

        stale = (self.refresh or not plain.exists()
                 or plain.stat().st_mtime < src.stat().st_mtime)
        if stale:
            t0 = time.time()
            ok = False
            for attempt in range(2):          # 微信边写边拷可能损坏，重试一次
                shutil.copyfile(src, snap)
                self._decrypt_main(snap, plain)
                merged = 0
                wal_src = Path(str(src) + "-wal")
                if self.with_wal and wal_src.exists() and wal_src.stat().st_size > 32:
                    try:
                        shutil.copyfile(wal_src, wal_snap)
                        merged = self._apply_wal(snap, wal_snap, plain)
                        if merged and not self._integrity_ok(plain):
                            self.log(f"  [!] {name} WAL 合并后校验失败，回退到纯主库")
                            self._decrypt_main(snap, plain)
                            merged = 0
                    except Exception as e:
                        self.log(f"  [!] {name} WAL 合并跳过: {str(e)[:60]}")
                        merged = 0
                if self._check_db(plain):
                    ok = True
                    tail = f"（含 WAL {merged} 帧）" if merged else ""
                    self.log(f"  [✓] {name} 解密 {src.stat().st_size/1048576:.1f}MB "
                             f"{time.time()-t0:.1f}s{tail}")
                    break
                time.sleep(0.8)
            for p in (wal_snap,):
                if p.exists():
                    try:
                        p.unlink()
                    except Exception:
                        pass
            if not ok:
                self.log(f"  [!] {name} 解密后校验失败（微信正在写入），本次跳过该库")
                self._broken.add(rel)
                self._plain[rel] = None
                if plain.exists():
                    try:
                        plain.unlink()
                    except Exception:
                        pass
                return None
        self._plain[rel] = plain
        return plain

    @staticmethod
    def _check_db(plain: Path) -> bool:
        try:
            con = sqlite3.connect(str(plain))
            con.execute("SELECT count(*) FROM sqlite_master").fetchone()
            con.close()
            return True
        except Exception:
            return False

    def conn(self, rel: str) -> sqlite3.Connection:
        if rel in self._con:
            return self._con[rel]
        p = self.plain(rel)
        if p is None:
            raise RuntimeError(f"数据库暂不可用: {rel}")
        # check_same_thread=False：GUI/MCP 等多线程服务会跨线程复用连接，
        # 由调用方（gui/server.py 的数据 API）加锁串行化，保证同一时刻单线程使用
        con = sqlite3.connect(str(p), check_same_thread=False)
        con.row_factory = sqlite3.Row
        self._con[rel] = con
        return con

    def has(self, rel: str) -> bool:
        return (self.db_root / rel).exists()

    # -------------------------------------------------------------- 分片
    def shards(self) -> list[str]:
        if self._shards is not None:
            return self._shards
        out = []
        d = self.db_root / "message"
        if d.exists():
            for p in sorted(d.glob("message_*.db")):
                if re.match(r"^message_\d+\.db$", p.name):
                    out.append(f"message/{p.name}")
        self._shards = out or ["message/message_0.db"]
        return self._shards

    @staticmethod
    def table_for(username: str) -> str:
        return "Msg_" + hashlib.md5(username.encode()).hexdigest()

    # -------------------------------------------------------------- 联系人
    def contacts(self) -> dict[str, dict]:
        if self._contacts is not None:
            return self._contacts
        out: dict[str, dict] = {}
        if self.has("contact/contact.db"):
            con = self.conn("contact/contact.db")
            try:
                for r in con.execute(
                        "SELECT username, nick_name, remark, alias, local_type, "
                        "delete_flag, verify_flag, big_head_url, small_head_url, description "
                        "FROM contact"):
                    d = dict(r)
                    u = d.get("username") or ""
                    if not u:
                        continue
                    out[u] = {
                        "wxid": u,
                        "nickname": d.get("nick_name") or "",
                        "remark": d.get("remark") or "",
                        "alias": d.get("alias") or "",
                        "avatarUrl": d.get("big_head_url") or d.get("small_head_url") or "",
                        "local_type": d.get("local_type"),
                        "delete_flag": d.get("delete_flag"),
                        "verify_flag": d.get("verify_flag"),
                        "description": d.get("description") or "",
                    }
            except Exception as e:
                self.log(f"  [!] 读取联系人失败: {str(e)[:60]}")
        self._contacts = out
        return out

    def display_name(self, username: str) -> str:
        c = self.contacts().get(username)
        if not c:
            return username
        return c.get("remark") or c.get("nickname") or username

    def contact_type(self, username: str) -> str:
        if username.endswith("@chatroom"):
            return "group"
        if username.startswith("gh_") or username.startswith("wxid_gh"):
            return "mp"
        c = self.contacts().get(username)
        if c:
            return "former_friend" if c.get("delete_flag") else "friend"
        return "other"

    def self_username(self) -> str:
        """本机 wxid。账号配置里存了 wxid 就用它（多账号时更可靠），否则从目录名推断。"""
        cfg_wxid = (self.account or {}).get("wxid")
        if cfg_wxid:
            return cfg_wxid
        name = self.user_dir.name
        cand = re.sub(r"_e[0-9a-zA-Z]{2,}$", "", name)
        for c in (cand, name):
            try:
                con = self.conn(self.shards()[0])
                row = con.execute("SELECT rowid FROM Name2Id WHERE user_name=?", (c,)).fetchone()
                if row:
                    return c
            except Exception:
                pass
        return cand

    def self_id(self, shard: str) -> int:
        if shard in self._self_ids:
            return self._self_ids[shard]
        su = self.self_username()
        sid = 0
        try:
            con = self.conn(shard)
            row = con.execute("SELECT rowid FROM Name2Id WHERE user_name=?", (su,)).fetchone()
            if row:
                sid = row[0]
            else:
                row = con.execute(
                    "SELECT rowid FROM Name2Id WHERE user_name LIKE 'wxid_%' ORDER BY rowid LIMIT 1"
                ).fetchone()
                sid = row[0] if row else 0
        except Exception:
            sid = 0
        self._self_ids[shard] = sid
        return sid

    def sender_names(self, shard: str) -> dict[int, str]:
        try:
            con = self.conn(shard)
            return {r["rowid"]: self.display_name(r["user_name"])
                    for r in con.execute("SELECT rowid, user_name FROM Name2Id")}
        except Exception:
            return {}

    # -------------------------------------------------------------- 会话索引
    def _index_path(self) -> Path:
        return CACHE / "session_index.json"

    def _build_session_index(self) -> list[dict]:
        """扫描所有消息分片，合并同一会话（微信会把旧数据留在 message_1 等分片）。"""
        merged: dict[str, dict] = {}
        for shard in self.shards():
            try:
                con = self.conn(shard)
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            except Exception as e:
                self.log(f"  [!] 跳过分片 {shard}: {str(e)[:60]}")
                continue
            try:
                name2id = list(con.execute("SELECT rowid, user_name, is_session FROM Name2Id"))
            except Exception:
                continue
            for r in name2id:
                u = r["user_name"]
                if not u:
                    continue
                t = self.table_for(u)
                if t not in tables:
                    continue
                try:
                    n = con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                except Exception:
                    continue
                e = merged.setdefault(u, {"sessionId": u, "messageCount": 0, "shards": []})
                e["messageCount"] += n
                e["shards"].append({"shard": shard, "table": t, "count": n})
        for e in merged.values():
            e["shards"].sort(key=lambda x: -x["count"])
            e["shard"] = e["shards"][0]["shard"]
            e["table"] = e["shards"][0]["table"]
        return list(merged.values())

    def sessions(self, refresh: bool = False) -> list[dict]:
        if self._sessions is not None and not refresh:
            return self._sessions
        idx_path = self._index_path()
        rows = None
        if not refresh and idx_path.exists():
            try:
                meta = json.loads(idx_path.read_text(encoding="utf-8"))
                sig = self._index_signature()
                if meta.get("signature") == sig:
                    rows = meta["sessions"]
            except Exception:
                rows = None
        if rows is None:
            rows = self._build_session_index()
            try:
                sig = self._index_signature() + f"|broken={len(self._broken)}"
                idx_path.write_text(json.dumps({"signature": sig, "sessions": rows},
                                               ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

        # 用 session.db 补齐摘要 / 最后时间 / 类型
        extra: dict[str, dict] = {}
        if self.has("session/session.db"):
            try:
                con = self.conn("session/session.db")
                for r in con.execute(
                        "SELECT username, type, summary, last_timestamp, sort_timestamp, "
                        "last_msg_sender, last_sender_display_name, unread_count, last_msg_type "
                        "FROM SessionTable"):
                    d = dict(r)
                    extra[d["username"]] = d
            except Exception as e:
                self.log(f"  [!] 读取 session.db 失败: {str(e)[:60]}")

        out = []
        for r in rows:
            u = r["sessionId"]
            e = extra.get(u, {})
            out.append({
                "sessionId": u,
                "displayName": self.display_name(u),
                "type": self._session_type(u, e.get("type")),
                "lastMessage": (e.get("summary") or "")[:120],
                "lastTime": e.get("last_timestamp") or 0,
                "sortTime": e.get("sort_timestamp") or 0,
                "unread": e.get("unread_count") or 0,
                "messageCount": r["messageCount"],
                "shard": r.get("shard"),
                "table": r.get("table"),
                "shards": r.get("shards") or [{"shard": r.get("shard"),
                                               "table": r.get("table"),
                                               "count": r["messageCount"]}],
            })
        out.sort(key=lambda x: (x["sortTime"] or x["lastTime"]), reverse=True)
        self._sessions = out
        return out

    def _index_signature(self) -> str:
        parts = ["v2"]
        for shard in self.shards():
            p = self.db_root / shard
            st = p.stat()
            # mtime 取 5 分钟粒度：微信持续写入，避免每次命令都重建索引
            parts.append(f"{shard}:{st.st_size}:{int(st.st_mtime) // 300}")
        return "|".join(parts) + f"|{self.user_dir}"

    @staticmethod
    def _session_type(username: str, session_type=None) -> str:
        """会话类型。与 contact_type() 的公众号判定保持一致。

        `wxid_gh*` 是公众号的 wxid 形式。漏判它会让同一个账号在「数据浏览」页
        显示「单聊」、在「联系人」页显示「公众号」——用户直接可见的自相矛盾。
        （前端曾用同一张中文映射表把 private 翻成「好友」，恰好把这个矛盾盖住了。）

        两套词表本身**不合并**：通讯录看 delete_flag、会话看 session_type，
        各有存在理由，合并会丢信息。这里只对齐公众号这一个判定。
        """
        if username.endswith("@chatroom"):
            return "group"
        if username.startswith("gh_") or username.startswith("wxid_gh"):
            return "mp"
        if session_type in (2, 3):
            return "group" if session_type == 2 else "mp"
        return "private"

    def find_session(self, query: str) -> dict | None:
        """按 wxid / 备注 / 昵称 模糊定位会话，命中多个取消息最多的。"""
        ss = self.sessions()
        q = (query or "").strip().lower()
        exact = [s for s in ss if s["sessionId"].lower() == q or s["displayName"] == query]
        if exact:
            return max(exact, key=lambda s: s["messageCount"])
        hits = [s for s in ss if q in s["displayName"].lower() or q in s["sessionId"].lower()]
        if not hits:
            return None
        return max(hits, key=lambda s: s["messageCount"])

    def find_sessions(self, query: str) -> list[dict]:
        ss = self.sessions()
        q = (query or "").strip().lower()
        if not q:
            return ss
        return [s for s in ss if q in s["displayName"].lower() or q in s["sessionId"].lower()]

    # -------------------------------------------------------------- 消息
    def _shard_list(self, sess: dict) -> list[dict]:
        return sess.get("shards") or [{"shard": sess["shard"], "table": sess["table"]}]

    @staticmethod
    def _dedup_key(row: sqlite3.Row) -> tuple:
        c = row["message_content"]
        if isinstance(c, (bytes, bytearray)):
            c = hashlib.md5(bytes(c[:256])).hexdigest()
        return (row["create_time"], row["local_type"], c)

    def raw_messages(self, sess: dict, limit: int = 50, offset: int = 0,
                     from_ts=None, to_ts=None, direction: str | None = None,
                     cursor=None, order: str = "desc") -> list[tuple[str, sqlite3.Row]]:
        """跨分片读取消息，返回 [(shard, row)]，已去重并按时间排序。"""
        need = max(1, int(limit) + int(offset))
        buckets: list[tuple[str, list[sqlite3.Row]]] = []
        for sh in self._shard_list(sess):
            con = self.conn(sh["shard"])
            where, params = [], []
            if from_ts:
                where.append("create_time >= ?")
                params.append(from_ts)
            if to_ts:
                where.append("create_time <= ?")
                params.append(to_ts)
            if cursor:
                where.append("sort_seq > ?")
                params.append(int(cursor))
            if direction == "out":
                where.append("real_sender_id = ?")
                params.append(self.self_id(sh["shard"]))
            elif direction == "in":
                where.append("real_sender_id != ?")
                params.append(self.self_id(sh["shard"]))
            sql = ('SELECT local_id, server_id, local_type, sort_seq, real_sender_id, create_time, '
                   'message_content, packed_info_data FROM "%s"' % sh["table"])
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY sort_seq " + ("ASC" if order == "asc" else "DESC")
            sql += " LIMIT ?"
            params.append(need)
            buckets.append((sh["shard"], con.execute(sql, params).fetchall()))

        seen: set = set()
        merged: list[tuple[str, sqlite3.Row]] = []
        for shard, rows in buckets:
            for r in rows:
                k = self._dedup_key(r)
                if k in seen:
                    continue
                seen.add(k)
                merged.append((shard, r))
        merged.sort(key=lambda x: (x[1]["create_time"], x[1]["sort_seq"]),
                    reverse=(order == "desc"))
        page = merged[int(offset):int(offset) + int(limit)]
        return page

    def to_message(self, row: sqlite3.Row, sess: dict, names: dict[int, str] | None = None,
                   shard: str | None = None) -> dict:
        m = parse_message(row["local_type"], row["message_content"], row["packed_info_data"])
        shard = shard or sess.get("shard")
        sid = self.self_id(shard)
        rid = row["real_sender_id"]
        sender = None
        if names:
            sender = names.get(rid)
        direction = "out" if (sid and rid == sid) else ("in" if sid else "unknown")
        return {
            "localId": row["local_id"],
            "serverId": row["server_id"],
            "createTime": row["create_time"],
            "sortSeq": row["sort_seq"],
            "direction": direction,
            "senderUsername": sender if sender and sender != sess["sessionId"] else None,
            "senderName": sender,
            "type": m["type"],
            "kind": KIND_BY_TYPE.get(m["type"], "other"),
            "content": m.get("text") or "",
            "imgMd5": m.get("img_md5"),
            "videoId": m.get("video_id"),
            "voiceMs": m.get("voice_ms"),
            "voiceText": m.get("voice_text"),
            "title": m.get("title"),
            "url": m.get("url"),
            "fileName": m.get("file_name"),
            "sessionId": sess["sessionId"],
            "sessionName": sess["displayName"],
        }

    def messages(self, query: str, limit: int = 50, offset: int = 0, from_ts=None, to_ts=None,
                 type_: str | None = None, direction: str | None = None, cursor=None,
                 order: str = "desc", with_raw=False) -> dict:
        sess = self.find_session(query) if isinstance(query, str) else query
        if not sess:
            raise SystemExit(f"[x] 没找到会话「{query}」，先跑 `mosu sessions` 看看")
        t = type_.lower() if type_ else None

        def build(take: int):
            rows = self.raw_messages(sess, limit=take, offset=offset, from_ts=from_ts,
                                     to_ts=to_ts, direction=direction, cursor=cursor, order=order)
            names_cache: dict[str, dict[int, str]] = {}
            out = []
            for shard, r in rows:
                if shard not in names_cache:
                    names_cache[shard] = self.sender_names(shard)
                m = self.to_message(r, sess, names_cache[shard], shard=shard)
                if t and m["kind"] != t:
                    continue
                out.append(m)
            return rows, out

        rows, msgs = build(limit)
        if t:
            factor = 2
            while len(msgs) < limit and factor <= 16:
                rows, msgs = build(limit * factor)
                factor *= 2
            msgs = msgs[:limit]
        if order == "desc":
            msgs.reverse()
        out = {
            "session": {"sessionId": sess["sessionId"], "displayName": sess["displayName"],
                        "type": sess["type"], "messageCount": sess["messageCount"],
                        "shards": [s["shard"] for s in self._shard_list(sess)]},
            "messages": msgs,
            "cursor": str(rows[-1][1]["sort_seq"]) if rows else None,
        }
        return out

    def collect_session_rows(self, sess: dict, from_ts=None, to_ts=None):
        """跨分片收集一个会话的消息（可限定时间范围），并把 sender id
        统一到同一命名空间（self=0，其他按分片基址偏移，避免跨分片撞号）。

        返回 (rows, names, self_id)：rows 每条带归一化后的 real_sender_id，
        names 为 {gid: 显示名}。CLI 导出与 GUI 导出共用。
        """
        base_of = {sh["shard"]: (i + 1) * 10_000_000
                   for i, sh in enumerate(self._shard_list(sess))}
        out_rows, names, self_ids = [], {}, set()
        for shard, r in self.iter_session_rows(sess, from_ts, to_ts):
            d = dict(r)
            sid = d.get("real_sender_id")
            if sid == self.self_id(shard):
                gid = 0
                self_ids.add(0)
                names[0] = self.display_name(self.self_username())
            else:
                gid = base_of.get(shard, 0) + (sid or 0)
                if gid not in names:
                    names[gid] = self.sender_names(shard).get(sid) or str(sid)
            d["real_sender_id"] = gid
            out_rows.append(d)
        out_rows.sort(key=lambda d: (d.get("create_time") or 0, d.get("sort_seq") or 0))
        return out_rows, names, 0

    def iter_session_rows(self, sess: dict, from_ts=None, to_ts=None,
                          batch: int = 2000, desc: bool = False, limit: int = 0):
        """全量游标式读取一个会话（用于搜索/统计），跨分片 yield (shard, row)。

        desc=True + limit>0 时按 sort_seq 倒序并限量 —— 采样"最近 N 条"时用，
        否则上游会拿到会话**最早**的 N 条（ORDER BY 默认升序）。
        """
        for sh in self._shard_list(sess):
            con = self.conn(sh["shard"])
            where, params = [], []
            if from_ts:
                where.append("create_time >= ?")
                params.append(from_ts)
            if to_ts:
                where.append("create_time <= ?")
                params.append(to_ts)
            sql = ('SELECT local_id, server_id, local_type, sort_seq, real_sender_id, create_time, '
                   'message_content, packed_info_data FROM "%s"' % sh["table"])
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY sort_seq DESC" if desc else " ORDER BY sort_seq"
            if limit and int(limit) > 0:
                sql += " LIMIT %d" % int(limit)
            cur = con.execute(sql, params)
            while True:
                rows = cur.fetchmany(batch)
                if not rows:
                    break
                for r in rows:
                    yield sh["shard"], r

    # -------------------------------------------------------------- 搜索
    def search(self, keyword: str, session: str | None = None, limit: int = 20,
               from_ts=None, to_ts=None, max_sessions=None) -> dict:
        targets = [self.find_session(session)] if session else self.sessions()
        targets = [t for t in targets if t]
        if max_sessions:
            targets = targets[:max_sessions]
        hits, total = [], 0
        kw = keyword.lower()
        for sess in targets:
            names_cache: dict[str, dict[int, str]] = {}
            for shard, r in self.iter_session_rows(sess, from_ts, to_ts):
                if shard not in names_cache:
                    names_cache[shard] = self.sender_names(shard)
                m = self.to_message(r, sess, names_cache[shard], shard=shard)
                blob = m["content"] or ""
                if kw not in blob.lower():
                    continue
                total += 1
                if len(hits) < limit:
                    hits.append(m)
        return {"messages": hits, "total": total,
                "sessionId": targets[0]["sessionId"] if len(targets) == 1 else None}
