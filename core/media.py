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
        # xor_key 从尾部反推（JPEG 以 FF D9 结束），反推失败则用全局 0x3D
        xk = raw[-2] ^ 0xFF if raw[-1] ^ 0xD9 == 0 or True else XOR_KEY
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

    def build(self, use_cache: bool = True) -> "MediaStore":
        if self._built:
            return self
        cache = self.cache_dir / "media_index.json" if self.cache_dir else None
        if use_cache and cache and cache.exists():
            try:
                d = json.loads(cache.read_text(encoding="utf-8"))
                # 缓存失效：目录指纹对不上（微信又存了新图）就重建
                if d.get("fingerprint") == self._dir_fingerprint():
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
