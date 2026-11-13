"""
墨溯 · 微信 4.x 本地媒体文件定位与解密

实测（微信 4.1.15.13）目录布局：
    msg/attach/<dir_md5>/<YYYY-MM>/Img/<md5>_W.dat   大图（XOR 0x3D 后即标准 JPEG/PNG）
    msg/attach/<dir_md5>/<YYYY-MM>/Img/<md5>_t.dat   缩略图（"V2" 私有容器，暂不用）
    msg/video/<YYYY-MM>/<video_id>.mp4               视频（明文）
    msg/video/<YYYY-MM>/<video_id>_thumb.jpg         视频封面
    msg/file/<YYYY-MM>/<原文件名>                     接收的文件（明文）

解密：微信 4.1 的大图 .dat 是 **单字节 XOR 0x3D**（实测 c2e5c2dd ^ ffd8ffe0 = 3d3d3d3d）。
语音（silk）不落本地磁盘 —— 只能拿到 packed_info_data 里的转文字。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

XOR_KEY = 0x3D

# V2 容器（微信 2025-08 起的主流格式）
V2_MAGIC = b"\x07\x08V2\x08\x07"
# 布局: [0:6]魔数 | [6:10]aes_size | [10:14]xor_size | [14]标志
#       | [15:15+aes_size] AES-128-ECB 密文 | 16 字节分隔 | XOR 尾部
# 账号级 AES key 放在 .cache/img_aes_key.txt（由 scripts/find_img_key.py 搜出）
_AES_KEY: bytes | None = None
_AES_KEY_LOADED = False


def set_aes_key(key: bytes | str | None) -> None:
    global _AES_KEY, _AES_KEY_LOADED
    if isinstance(key, str):
        key = bytes.fromhex(key.strip())
    _AES_KEY, _AES_KEY_LOADED = key, True


def _load_aes_key():
    """惰性加载账号级 AES key（图片 V2 解密用）。

    顺序：环境变量 MOSU_IMG_KEY_HEX > 墨溯密钥库 imgKeyHex（~/.mosu）> 缓存文件
          > 缓存文件（项目 .cache 或 ~/.mosu/img_aes_key.txt）
    """
    global _AES_KEY, _AES_KEY_LOADED
    if _AES_KEY_LOADED:
        return _AES_KEY
    _AES_KEY_LOADED = True
    env = os.environ.get("MOSU_IMG_KEY_HEX", "").strip()
    if env:
        try:
            _AES_KEY = bytes.fromhex(env)
            return _AES_KEY
        except ValueError:
            pass
    vaults = [Path(__file__).resolve().parent.parent / ".cache" / "img_aes_key.txt",
              Path(os.environ.get("MOSU_HOME") or (Path.home() / ".mosu")) / "img_aes_key.txt",
              Path.home() / ".miyu" / "img_aes_key.txt"]
    for p in vaults:
        try:
            if p.exists():
                _AES_KEY = bytes.fromhex(p.read_text(encoding="utf-8").strip())
                break
        except Exception:
            continue
    # 最后回退：账号配置里的 imgKeyHex（与 db key 同库，最持久）
    if _AES_KEY is None:
        try:
            from core.dataset import resolve_account
            _, acc = resolve_account()
            if acc.get("imgKeyHex"):
                _AES_KEY = bytes.fromhex(acc["imgKeyHex"])
        except Exception:
            pass
    return _AES_KEY


def decode_v2(raw: bytes, aes_key: bytes | None = None) -> bytes | None:
    """解 V2 容器：AES-ECB 头 + XOR 尾。key 不对时返回 None。"""
    if raw[:6] != V2_MAGIC:
        return None
    key = aes_key or _load_aes_key()
    if not key:
        return None
    import struct
    from Crypto.Cipher import AES
    try:
        aes_size, = struct.unpack_from("<I", raw, 6)
        xor_size, = struct.unpack_from("<I", raw, 10)
        if 15 + aes_size + 16 + xor_size != len(raw) or aes_size == 0:
            return None
        head = AES.new(key, AES.MODE_ECB).decrypt(raw[15 : 15 + aes_size])[:aes_size]
        # 尾部是固定单字节 XOR（全局 0x3D）。曾有一段「从 JPEG 尾标记反推 xor_key」
        # 的代码，但条件恒真且结果从未被使用，等于死代码 —— 已删除。
        tail = bytes(b ^ XOR_KEY for b in raw[15 + aes_size + 16 :])
        return head + tail
    except Exception:
        return None

# 图片魔数 -> 扩展名
_MAGIC = [
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"RIFF", "webp"),
    (b"\x00\x00\x00\x20ftyp", "heic"),
]


def decode_dat(raw: bytes) -> bytes:
    """微信 4.1 图片 .dat -> 明文图像字节（XOR 0x3D）。"""
    return bytes(b ^ XOR_KEY for b in raw)


def sniff_ext(data: bytes) -> str:
    """按魔数猜图片扩展名。"""
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    if data[4:8] == b"ftyp":
        return "heic"
    return "bin"


class EmoticonStore:
    """本地「已解密表情」目录：md5 命名、明文 GIF/PNG/JPG。

    有些机器上已有一份从微信 CDN 下载好的明文表情（例如早前的导出产物），
    文件名就是 md5。这里只读查找，不联网、不写文件、不猜密钥。
    加密缓存的解密在 EncryptedEmoticonStore，两者由调用方按序兜底。

    目录来源（按序）：
      1. 环境变量 MOSU_EMOTICON_DIRS（os.pathsep 分隔）
      2. 项目同级目录自动发现（<项目父目录>/*/Emojis）
    不硬编码任何个人路径；找不到就返回 None，由调用方回退到占位文案。
    """

    EXTS = (".gif", ".png", ".jpg", ".jpeg", ".webp")

    def __init__(self, dirs: list[Path] | None = None):
        self.dirs = dirs if dirs is not None else self._discover()
        self._index: dict[str, Path] | None = None

    @staticmethod
    def _discover() -> list[Path]:
        dirs: list[Path] = []
        env = os.environ.get("MOSU_EMOTICON_DIRS", "").strip()
        if env:
            dirs += [Path(p) for p in env.split(os.pathsep) if p.strip()]
        # 项目根目录的**同级**目录下找 Emojis（不递归进无关深层目录）
        try:
            parent = Path(__file__).resolve().parent.parent.parent
            dirs += [d for d in parent.glob("*/Emojis") if d.is_dir()]
        except Exception:
            pass
        # 去重且只保留真实存在的目录
        seen, out = set(), []
        for d in dirs:
            try:
                key = str(d.resolve())
                if key not in seen and d.is_dir():
                    seen.add(key)
                    out.append(d)
            except Exception:
                continue
        return out

    def _build(self) -> dict[str, Path]:
        idx: dict[str, Path] = {}
        for d in self.dirs:
            try:
                for p in d.rglob("*"):
                    if not p.is_file() or p.suffix.lower() not in self.EXTS:
                        continue
                    stem = p.stem.lower()
                    # 只认 32 位 hex，避免把无关图片当表情
                    if len(stem) == 32 and all(c in "0123456789abcdef" for c in stem):
                        idx.setdefault(stem, p)
            except Exception:
                continue
        return idx

    def get(self, md5: str | None) -> tuple[bytes, str, str] | None:
        """返回 (明文图片字节, 扩展名, 来源标记)，找不到返回 None。"""
        if not md5 or len(md5) != 32:
            return None
        if self._index is None:
            self._index = self._build()
        p = self._index.get(md5.lower())
        if not p:
            return None
        try:
            data = p.read_bytes()
        except Exception:
            return None
        ext = "." + sniff_ext(data)
        if ext == ".bin":
            return None       # 不是可显示的图片，别把垃圾喂给 <img>
        return (data, ext, "emoticon")


class EncryptedEmoticonStore:
    """微信 4.x 加密表情缓存的本地解密（只读）。

    格式（社区对 Weixin.dll 的静态逆向，已在真实账号上实测 495/495 通过）：
      整文件 AES-128-CBC，key = IV = MD5(f"{seed}{wxid}EMOTICON").digest()
      PKCS#7 填充，密文长度必为 16 的倍数，无文件头。
      与图片 V2 的 AES key 不同但同源 seed；seed 可只读扫进程内存得到，
      也可离线穷举 0..2^32（MD5 单核约 190 万次/秒）。
      wxid 要去掉账号目录末尾的 4 位 hex 后缀。

    解密后的明文可能是 PNG/GIF/JPEG/WebP（直接可显示），也可能是
    wxgf（微信私有 HEVC 容器）。后者用本机 ffmpeg 抽**首帧**转 PNG——
    动画与 alpha 会丢，来源标记 emoticon-wxgf 明确写出这条限制，
    不把静态首帧伪装成完整表情。ffmpeg 不可用或转码失败则回退占位。

    目录：账号目录下 business/emoticon/{Persist,Thumb}、cache/*/Emoticon。
    """

    CACHE_SUBDIRS = ("business/emoticon/Persist", "business/emoticon/Thumb")

    def __init__(self, user_dir: str | os.PathLike, seed: str | None = None,
                 wxid: str | None = None,
                 cache_dir: str | os.PathLike | None = None):
        self.user_dir = Path(user_dir)
        self.wxid = wxid or self._wxid_from_dir()
        self.seed = seed
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._key: bytes | None = None
        self._index: dict[str, Path] | None = None

    def _wxid_from_dir(self) -> str:
        # 账号目录形如 wxid_xxxx_e027，派生用的是去掉 _xxxx 后缀的 wxid
        name = self.user_dir.name
        return name.rsplit("_", 1)[0] if "_" in name else name

    def _derive(self, seed: str) -> bytes:
        import hashlib
        # MD5 是微信既定 KDF，不能换 SHA-256（换了与微信派生结果不符）
        return hashlib.md5((str(seed) + self.wxid + "EMOTICON").encode()).digest()

    def set_seed(self, seed: str) -> None:
        self.seed = str(seed)
        self._key = self._derive(self.seed)

    def _key_ok(self, probe: Path) -> bool:
        """用首个候选文件验证 key 真的能解出图片（不信任硬编码 key）。"""
        if not self.seed:
            return False
        key = self._derive(self.seed)
        try:
            raw = probe.read_bytes()
            if len(raw) % 16 or len(raw) < 32:
                return False
            dec = _aes_cbc_decrypt(key, raw)
            return _strip_pkcs7(dec) is not None and sniff_ext(_strip_pkcs7(dec)) != "bin"
        except Exception:
            return False
        finally:
            self._key = key

    def _build(self) -> dict[str, Path]:
        idx: dict[str, Path] = {}
        roots = [self.user_dir / s for s in self.CACHE_SUBDIRS]
        cache_root = self.user_dir / "cache"
        if cache_root.is_dir():
            roots += [d for d in cache_root.glob("*/Emoticon") if d.is_dir()]
        for d in roots:
            if not d.is_dir():
                continue
            for p in d.rglob("*"):
                if not p.is_file():
                    continue
                stem = p.stem.lower()
                if len(stem) == 32 and all(c in "0123456789abcdef" for c in stem):
                    idx.setdefault(stem, p)
        return idx

    def get(self, md5: str | None) -> tuple[bytes, str, str] | None:
        if not md5 or len(md5) != 32 or not self.seed:
            return None
        if self._index is None:
            self._index = self._build()
        p = self._index.get(md5.lower())
        if not p:
            return None
        if self._key is None:
            self._key = self._derive(self.seed)
        try:
            raw = p.read_bytes()
            if len(raw) % 16:
                return None
            dec = _strip_pkcs7(_aes_cbc_decrypt(self._key, raw))
            if not dec:
                return None
            ext = sniff_ext(dec)
            if ext != "bin":
                return (dec, "." + ext, "emoticon-dec")
            # wxgf：微信私有 HEVC 容器，浏览器不认。用 ffmpeg 抽首帧转 PNG。
            # 注意这里只能得到**静态首帧**，动画/透明度会丢——如实标注，
            # 不假装是完整表情。结果落磁盘缓存，避免每个 <img> 都跑一次 ffmpeg。
            if dec[:4] == b"wxgf":
                return self._wxgf_to_png(md5, dec)
            return None
        except Exception:
            return None

    def _wxgf_to_png(self, md5: str, dec: bytes) -> tuple[bytes, str, str] | None:
        import shutil
        import subprocess
        import tempfile
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None
        cache = (self.cache_dir / "emoticon_wxgf") if self.cache_dir else None
        if cache:
            hit = cache / (md5.lower() + ".png")
            try:
                if hit.exists():
                    return (hit.read_bytes(), ".png", "emoticon-wxgf")
            except Exception:
                pass
        # WXGF 头之后是 Annex-B HEVC NALU；起始码 00 00 00 01 的位置即裸流起点
        start = dec.find(b"\x00\x00\x00\x01")
        if start < 0:
            return None
        try:
            with tempfile.TemporaryDirectory() as td:
                src, dst = Path(td) / "s.h265", Path(td) / "o.png"
                src.write_bytes(dec[start:])
                r = subprocess.run(
                    [ffmpeg, "-y", "-loglevel", "error", "-f", "hevc",
                     "-i", str(src), "-frames:v", "1", "-update", "1", str(dst)],
                    capture_output=True, timeout=25)
                if r.returncode != 0 or not dst.exists():
                    return None
                data = dst.read_bytes()
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                return None
            if cache:
                try:
                    cache.mkdir(parents=True, exist_ok=True)
                    (cache / (md5.lower() + ".png")).write_bytes(data)
                except Exception:
                    pass
            return (data, ".png", "emoticon-wxgf")
        except Exception:
            return None


def _aes_cbc_decrypt(key: bytes, raw: bytes) -> bytes:
    from Crypto.Cipher import AES
    return AES.new(key, AES.MODE_CBC, key).decrypt(raw)


def _strip_pkcs7(data: bytes) -> bytes | None:
    if not data:
        return None
    pad = data[-1]
    if not 1 <= pad <= 16 or data[-pad:] != bytes([pad]) * pad:
        return None
    return data[:-pad]


def build_emoticon_getter(user_dir, cache_dir=None, wxid=None, img_key_hex=None):
    """构造导出/接口共用的表情兜底回调：md5 → (字节, ".png", 来源)。

    顺序与 /api/media 一致：本机明文缓存 → 微信加密缓存解密。
    没有 seed（微信没运行且没配 MOSU_EMOTICON_SEED）时仍能用明文缓存那一路。
    返回的闭包惰性建索引，第一次调用才有 IO。
    """
    user_dir = Path(user_dir)
    _wxid = wxid or user_dir.name.rsplit("_", 1)[0]
    _key_hex = img_key_hex
    if _key_hex is None:
        try:
            from core.dataset import resolve_account
            _, acc = resolve_account()
            _key_hex = acc.get("imgKeyHex") or ""
        except Exception:
            _key_hex = ""
    plain = EmoticonStore()
    enc = EncryptedEmoticonStore(
        user_dir, seed=find_emoticon_seed(_wxid, _key_hex), wxid=_wxid,
        cache_dir=cache_dir)

    def get(md5):
        return plain.get(md5) or enc.get(md5)

    return get


def find_emoticon_seed(wxid: str, img_key_hex: str = "") -> str | None:
    """只读扫微信进程内存找 seed，零误报判定。

    判定式：md5(f"{seed}{wxid}").hexdigest()[:16] == 已知 V2 图片 key。
    后者可从账号配置的 imgKeyHex 解出（bytes.fromhex 后是 ASCII 串）。
    找不到返回 None——不猜、不硬编码、不联网。
    """
    import hashlib
    import re

    env = os.environ.get("MOSU_EMOTICON_SEED", "").strip()
    if env:
        return env
    if not img_key_hex:
        return None
    try:
        target = bytes.fromhex(img_key_hex).decode()
    except Exception:
        return None

    try:
        from core import keyfinder as kf
    except Exception:
        return None
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process Weixin -ErrorAction SilentlyContinue | "
             "Select-Object -ExpandProperty Id"],
            capture_output=True, text=True, timeout=10).stdout
        pids = [int(x) for x in re.findall(r"\d+", out)]
    except Exception:
        return None

    cand = re.compile(rb"(?<![0-9])(\d{8,12})(?![0-9])")
    seen: set[str] = set()
    for pid in pids:
        try:
            for _base, _prot, buf in kf.iter_regions(pid, max_region=256 * 1024 * 1024):
                for m in cand.finditer(buf):
                    s = m.group(1).decode()
                    if s in seen:
                        continue
                    seen.add(s)
                    # MD5 是微信既定 KDF，不能替换
                    if hashlib.md5((s + wxid).encode()).hexdigest()[:16] == target:
                        return s
        except Exception:
            continue
    return None


class MediaStore:
    """本地媒体文件索引 + 解密取字节。"""

    def __init__(self, user_dir: str | os.PathLike, cache_dir: str | os.PathLike | None = None):
        self.user_dir = Path(user_dir)
        self.msg_dir = self.user_dir / "msg"
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.images: dict[str, dict[str, str]] = {}   # md5 -> {'W': path, 't': path}
        self.videos: dict[str, str] = {}              # video_id -> mp4 path
        self.thumbs: dict[str, str] = {}              # video_id -> thumb path
        self.files: dict[str, str] = {}               # 文件名 -> path
        self._built = False

    # ---------------------------------------------------------------- 索引
    def _dir_fingerprint(self) -> float:
        """attach 目录树的 mtime 指纹（只扫目录不读文件，几十个目录秒完）。
        微信新增图片会更新其所在目录 mtime —— 用它判断缓存是否过期。"""
        attach = self.msg_dir / "attach"
        if not attach.exists():
            return 0.0
        m = attach.stat().st_mtime
        for dp, dns, _ in os.walk(attach):
            for d in dns:
                try:
                    m = max(m, os.path.getmtime(os.path.join(dp, d)))
                except OSError:
                    pass
        return m

    def _cache_fingerprint(self) -> float:
        """cache 目录（微信 4.x 图片缓存）的 mtime 指纹。"""
        cdir = self.user_dir / "cache"
        if not cdir.exists():
            return 0.0
        m = cdir.stat().st_mtime
        for dp, dns, _ in os.walk(cdir):
            for d in dns:
                try:
                    m = max(m, os.path.getmtime(os.path.join(dp, d)))
                except OSError:
                    pass
        return m

    def build(self, use_cache: bool = True) -> "MediaStore":
        if self._built:
            return self
        cache = self.cache_dir / "media_index.json" if self.cache_dir else None
        if use_cache and cache and cache.exists():
            try:
                d = json.loads(cache.read_text(encoding="utf-8"))
                # 缓存失效：目录指纹对不上（微信又存了新图）就重建
                if d.get("fingerprint") == self._dir_fingerprint() \
                        and d.get("cache_fp") == self._cache_fingerprint():
                    self.images = d.get("images", {})
                    self.videos = d.get("videos", {})
                    self.thumbs = d.get("thumbs", {})
                    self.files = d.get("files", {})
                    self._built = True
                    return self
            except Exception:
                pass

        t0 = time.time()
        attach = self.msg_dir / "attach"
        if attach.exists():
            for dp, _, fn in os.walk(attach):
                for f in fn:
                    if "_" in f:
                        # {md5}_t.dat 缩略图 / {md5}_W.dat 等
                        stem, _, suffix = f.partition("_")
                        ext = suffix.split(".")[0].lower()
                    else:
                        # 无后缀主图 {md5}.dat —— 曾整个漏掉（6726 张），
                        # partition("_") 对无下划线文件返回整名（含 .dat）长度不对被跳过
                        stem, _, _ = f.partition(".")
                        ext = "main"
                    if len(stem) != 32:
                        continue
                    self.images.setdefault(stem.lower(), {})[ext] = os.path.join(dp, f)

        vdir = self.msg_dir / "video"
        if vdir.exists():
            for dp, _, fn in os.walk(vdir):
                for f in fn:
                    p = os.path.join(dp, f)
                    if f.endswith("_thumb.jpg"):
                        self.thumbs[f[:-10]] = p
                    elif f.endswith(".mp4"):
                        self.videos[os.path.splitext(f)[0]] = p

        fdir = self.msg_dir / "file"
        if fdir.exists():
            for dp, _, fn in os.walk(fdir):
                for f in fn:
                    self.files.setdefault(f, os.path.join(dp, f))

        # cache 目录（微信 4.x 图片缓存）：{md5} / {md5}.dat / {md5}_b.dat，
        # 命名规则与聊天图片一致，因此朋友圈按 md5 反查也能命中这里。
        cdir = self.user_dir / "cache"
        if cdir.exists():
            for dp, _, fn in os.walk(cdir):
                for f in fn:
                    stem, _, suffix = f.partition(".")
                    if len(stem) != 32:
                        continue
                    try:
                        int(stem, 16)
                    except ValueError:
                        continue
                    part = suffix.split("_")[0].lower() or "main"
                    self.images.setdefault(stem.lower(), {}) \
                        .setdefault(part, os.path.join(dp, f))

        self._built = True
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(
                json.dumps(
                    {"fingerprint": self._dir_fingerprint(),
                     "cache_fp": self._cache_fingerprint(),
                     "images": self.images, "videos": self.videos,
                     "thumbs": self.thumbs, "files": self.files},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        self.build_sec = round(time.time() - t0, 1)
        return self

    # ---------------------------------------------------------------- 取用
    def has_image(self, md5: str | None) -> bool:
        """本地是否存在该图片（用于决定走本地还是远程，不解密不读盘）。"""
        if not md5:
            return False
        return md5.lower() in self.images

    @staticmethod
    def _decode_file(path: str) -> tuple[bytes, str] | None:
        try:
            raw = Path(path).read_bytes()
        except Exception:
            return None
        dec = None
        if raw[:6] == V2_MAGIC:
            dec = decode_v2(raw)                     # 新一代：AES + XOR
        if dec is None:
            dec = decode_dat(raw)                    # 老格式：纯 XOR 0x3D
        ext = sniff_ext(dec)
        if ext == "bin":
            return None
        return dec, ext

    def explain_missing(self, md5: str | None) -> str:
        """取不到图时，给出人话原因——用户据此才能决定要不要去微信里刷一次。

        实测三类（全库 15992 张）：
          ① 索引里根本没有       → 微信没保存过这张图
          ② 有文件但解码失败     → 未下载占位文件（几百字节，头部 07 08 56…）
                                   全库 999 张属此类
          ③ 有文件且能解码       → 不该走到这里（说明是别的错误）
        """
        if not md5:
            return "缺少图片标识"
        entry = self.images.get(md5.lower())
        if not entry:
            return "微信没有保存过这张图（本地无记录）"
        for key, p in entry.items():
            if not p or not os.path.exists(p):
                continue
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            if size < 2000:
                return (f"微信只留了一个未下载的占位文件（{size} 字节），"
                        "原图从未下载到本机——在微信里点开这张图后再试")
        return "本地有文件但无法解码，可能已损坏"

    def _pick(self, md5: str | None, order: tuple[str, ...]) -> tuple[bytes, str, str] | None:
        """按 order 里的分区优先级取一张图，返回 (明文字节, 扩展名, 命中的分区名)。"""
        if not md5:
            return None
        entry = self.images.get(md5.lower())
        if not entry:
            return None
        for key in order:
            p = entry.get(key)
            if not p:
                continue
            got = self._decode_file(p)
            if got:
                return got[0], got[1], key
        return None

    def get_thumb_image(self, md5: str | None) -> tuple[bytes, str, str] | None:
        """缩略图：优先微信存的小图 _t；没有就退回主图（前端列表用，快优先）。"""
        return self._pick(md5, ("t", "main", "h", "w", "W"))

    def get_full_image(self, md5: str | None) -> tuple[bytes, str, str] | None:
        """原图/高清：_W > _h > 无后缀主图 > _t（本地只有缩略图时才用缩略图兜底）。"""
        return self._pick(md5, ("w", "W", "h", "main", "t"))

    def get_image(self, md5: str | None) -> tuple[bytes, str] | None:
        """兼容旧调用：等价于 get_full_image，返回 (明文图像字节, 扩展名)。"""
        got = self.get_full_image(md5)
        return (got[0], got[1]) if got else None

    def get_video(self, vid: str | None) -> str | None:
        if not vid:
            return None
        p = self.videos.get(vid)
        return p if p and os.path.exists(p) else None

    def get_thumb(self, vid: str | None) -> tuple[bytes, str] | None:
        if not vid:
            return None
        p = self.thumbs.get(vid)
        if not p or not os.path.exists(p):
            return None
        try:
            raw = Path(p).read_bytes()
        except Exception:
            return None
        if raw[:3] == b"\xff\xd8\xff" or raw[:4] == b"\x89PNG":
            return raw, sniff_ext(raw)
        dec = decode_dat(raw)
        return (dec, sniff_ext(dec)) if sniff_ext(dec) != "bin" else None

    def stats(self) -> dict:
        return {
            "图片(md5)": len(self.images),
            "视频": len(self.videos),
            "视频封面": len(self.thumbs),
            "文件": len(self.files),
        }
