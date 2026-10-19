# -*- coding: utf-8 -*-
"""朋友圈远程图片代理 + 缓存。

为什么需要它：微信 4.x 把朋友圈图片地址放在 `<url token="...">` 上，token 是
访问 qpic.cn 的必需参数。浏览器直接 <img src="裸地址"> 必然 403/空白 —— 页面
里既没有 token 也没带 Referer。这就是「朋友圈图片点不开」的根因。

做法：服务端按 md5 反查数据库里的地址（**不接受任意 URL**，杜绝 SSRF），
带上 token 与微信 Referer 抓取，落盘到 .cache/remote/，之后直接读缓存。
列表走缩略地址（…/150），点开走原图地址（…/0）。

只做零依赖实现（urllib + 标准库），不引 requests。
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 MicroMessenger/8.0")
REFERER = "https://weixin.qq.com/"

# 只允许微信官方图片域名（双保险：md5 反查已限定，这里再挡一层）
ALLOWED_HOSTS = ("qpic.cn", "qlogo.cn", "weixin.qq.com", "mmbiz.qpic.cn")

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, bytes, str]] = {}   # md5 -> (ts, data, ext)
_MEM_MAX = 64          # 内存里最多留几张（灯箱连看会复用）
_TTL = 3600 * 24 * 30   # 磁盘缓存保留 30 天


class RemoteError(RuntimeError):
    """带用户可读原因的错误。"""


def cache_dir(root: str | os.PathLike) -> Path:
    d = Path(root) / "remote"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _host_ok(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return False
    host = host.lower()
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def _with_token(url: str, token: str, key: str = "") -> str:
    """把 token / key 拼成查询参数。微信 CDN 同时看 URL 与 token，缺一不可。"""
    if not token:
        return url
    sep = "&" if "?" in url else "?"
    extra = f"token={urllib.parse.quote(token)}"
    if key:
        extra += f"&key={urllib.parse.quote(key)}"
    # 已有 token 就替换，别叠两个
    base = url.split("?")[0]
    return f"{base}{sep}{extra}"


def _fetch(url: str, timeout: int = 20) -> bytes:
    if not _host_ok(url):
        raise RemoteError("地址不在允许的微信图床域名内")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": REFERER,
        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        raise RemoteError(f"微信图床返回 {e.code}（多为 token 过期）") from e
    except Exception as e:
        raise RemoteError(f"网络失败：{type(e).__name__}") from e
    if len(data) < 128 or len(data) > 40 * 1024 * 1024:
        raise RemoteError("返回内容不是图片")
    return data


def _sniff(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"BM":
        return "bmp"
    raise RemoteError("不是可识别的图片格式")


def get(root: str | os.PathLike, md5: str, item: dict, thumb: bool = False,
        timeout: int = 20) -> tuple[bytes, str, str]:
    """取一张朋友圈图片。

    返回 (字节, 扩展名, 来源标记)。thumb=True 取缩略图地址，否则取原图地址。
    失败抛 RemoteError，由上层转成 404 JSON。
    """
    md5 = (md5 or "").strip().lower()
    url = (item.get("thumbUrl") if thumb else item.get("url")) or ""
    token = (item.get("thumbToken") or item.get("token")) if thumb else item.get("token")
    if not url:
        raise RemoteError("数据库里没有这张图的地址")
    key = hashlib.md5(f"{md5}|{'t' if thumb else 'f'}".encode()).hexdigest()[:16]
    cdir = cache_dir(root)

    # 1) 内存
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < _TTL:
            return hit[1], hit[2], "mem"

    # 2) 磁盘
    for ext in ("jpg", "png", "webp", "gif"):
        f = cdir / f"{key}.{ext}"
        if f.exists() and time.time() - f.stat().st_mtime < _TTL:
            data = f.read_bytes()
            with _LOCK:
                if len(_CACHE) >= _MEM_MAX:
                    _CACHE.pop(next(iter(_CACHE)))
                _CACHE[key] = (time.time(), data, ext)
            return data, ext, "disk"

    # 3) 抓取
    data = _fetch(_with_token(url, token, item.get("key") or ""), timeout=timeout)
    ext = _sniff(data)
    try:
        (cdir / f"{key}.{ext}").write_bytes(data)
    except Exception:
        pass
    with _LOCK:
        if len(_CACHE) >= _MEM_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = (time.time(), data, ext)
    return data, ext, "net"


def stats(root: str | os.PathLike) -> dict:
    d = cache_dir(root)
    files = list(d.glob("*"))
    return {"files": len(files),
            "bytes": sum(f.stat().st_size for f in files if f.is_file())}