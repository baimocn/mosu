"""
墨溯 · 聊天记录 HTML 导出器

产出：一个目录
    <out>/index.html      完整聊天记录（微信风格气泡）
    <out>/media/*         图片 / 视频 / 封面（已解密为明文 jpg/png/mp4）

设计取舍：
  - 媒体默认**落地为文件**而非 base64 内嵌（127 张图内嵌会让 HTML 膨胀到几百 MB）
  - 语音：微信 4.1 不把 silk 音频落本地磁盘，只能拿到 packed_info_data 里的转文字，
    因此渲染为「语音 + 时长 + 转文字」，不伪造音频
"""
from __future__ import annotations

import html
import os
import time
from datetime import datetime
from pathlib import Path

from media import MediaStore
from msgparse import (
    T_APPMSG, T_EMOJI, T_IMAGE, T_SYS, T_TEXT, T_VIDEO, T_VOICE,
    parse_message,
)

CSS = """
:root{--bg:#ededed;--self:#95ec69;--other:#fff;--line:#d9d9d9;--time:#b2b2b2;--name:#9e9e9e}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);font:15px/1.6 -apple-system,"Microsoft YaHei",Segoe UI,sans-serif;color:#191919}
.wrap{max-width:820px;margin:0 auto;padding:24px 16px 80px}
header{text-align:center;padding:18px 0 26px;border-bottom:1px solid var(--line);margin-bottom:18px}
header h1{margin:0 0 6px;font-size:20px;font-weight:600}
header .meta{color:#8c8c8c;font-size:12.5px}
.day{text-align:center;margin:22px 0 12px}
.day span{display:inline-block;background:rgba(0,0,0,.06);color:#8c8c8c;font-size:12px;padding:2px 10px;border-radius:4px}
.row{display:flex;margin-bottom:14px;align-items:flex-start;gap:10px}
.row.me{flex-direction:row-reverse}
.avatar{width:38px;height:38px;border-radius:4px;background:#c8c8c8;flex:0 0 38px;
  display:flex;align-items:center;justify-content:center;color:#fff;font-size:14px;overflow:hidden}
.avatar img{width:100%;height:100%;object-fit:cover}
.col{max-width:72%;min-width:0}
.name{font-size:12px;color:var(--name);margin:0 0 4px 2px}
.row.me .name{text-align:right;margin:0 2px 4px 0}
.bubble{position:relative;background:var(--other);border-radius:6px;padding:9px 12px;
  word-break:break-word;white-space:pre-wrap;box-shadow:0 1px 1px rgba(0,0,0,.04)}
.row.me .bubble{background:var(--self)}
.bubble::after{content:"";position:absolute;top:12px;left:-5px;border:5px solid transparent;border-right-color:var(--other)}
.row.me .bubble::after{left:auto;right:-5px;border-right-color:transparent;border-left-color:var(--self)}
.bubble img.media{max-width:100%;max-height:420px;border-radius:4px;display:block;cursor:zoom-in}
.bubble video{max-width:100%;max-height:420px;border-radius:4px;display:block;background:#000}
.card{display:flex;gap:10px;align-items:center;background:#fff;border-radius:6px;padding:10px;max-width:320px}
.card .icon{width:40px;height:40px;border-radius:4px;background:#f2f2f2;display:flex;
  align-items:center;justify-content:center;font-size:11px;color:#888;flex:0 0 40px}
.card .t{font-size:14px;line-height:1.35;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.card .s{font-size:11px;color:#aaa;margin-top:3px}
.voice{display:flex;align-items:center;gap:8px}
.voice .bar{width:14px;height:14px;border-right:3px solid #16a34a;border-radius:2px}
.voice .sec{font-size:12px;color:#888}
.voice .txt{font-size:12.5px;color:#555;margin-top:4px;border-left:2px solid #ddd;padding-left:6px}
.sys{text-align:center;font-size:12px;color:var(--time);margin:14px 0}
.plain{font-size:13px;color:#666}
footer{text-align:center;color:#aaa;font-size:12px;margin-top:40px;padding-top:18px;border-top:1px solid var(--line)}
"""

JS = """
document.addEventListener('click', function(e){
  var t = e.target;
  if (t.tagName === 'IMG' && t.classList.contains('media')) {
    var w = window.open(''); if(!w) return;
    w.document.write('<title>'+(t.alt||'图片')+'</title><body style="margin:0;background:#111;display:flex;align-items:center;justify-content:center;height:100vh">'
      + '<img src="'+t.src+'" style="max-width:100%;max-height:100%"></body>');
  }
});
"""


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def _fmt_time(ts: int) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def _fmt_day(ts: int) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y年%m月%d日")
    except Exception:
        return ""


class ChatExporter:
    def __init__(self, media: MediaStore, out_dir: str | os.PathLike, voice_map: dict | None = None):
        self.media = media
        self.out_dir = Path(out_dir)
        self.media_dir = self.out_dir / "media"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.voice_map = voice_map or {}
        self.stats = {"图片": 0, "图片缺失": 0, "视频": 0, "视频缺失": 0,
                      "语音": 0, "语音可播": 0, "文件": 0, "表情": 0}

    # ---------------------------------------------------------------- 媒体落地
    _IMG_EXTS = ("jpg", "jpeg", "png", "gif", "webp", "bmp")

    def _cached_media(self, rel_prefix: str, exts) -> str | None:
        """磁盘缓存命中检查：上次导出已落盘的媒体直接复用，跳过解密/拷贝。"""
        for e in exts:
            cand = self.out_dir / f"{rel_prefix}.{e}"
            if cand.exists():
                return f"{rel_prefix}.{e}"
        return None

    def _save_image(self, md5: str) -> str | None:
        prefix = f"media/{md5[:2]}/{md5}"
        hit = self._cached_media(prefix, self._IMG_EXTS)
        if hit:
            return hit
        got = self.media.get_image(md5)
        if not got:
            return None
        data, ext = got
        rel = f"{prefix}.{ext}"
        dst = self.out_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            dst.write_bytes(self._maybe_resize(data, ext))
        except Exception:
            return None
        return rel

    @staticmethod
    def _maybe_resize(data: bytes, ext: str, max_width: int = 1280) -> bytes:
        """图片过宽时等比缩放，避免导出目录爆炸（无 Pillow 则原样返回）。"""
        if len(data) < 300 * 1024:
            return data
        try:
            import io
            from PIL import Image  # type: ignore
            im = Image.open(io.BytesIO(data))
            if im.width <= max_width:
                return data
            h = int(im.height * max_width / im.width)
            im = im.convert("RGB") if im.mode in ("P", "RGBA") else im
            im = im.resize((max_width, h), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=82, optimize=True)
            return buf.getvalue()
        except Exception:
            return data

    def _save_video(self, vid: str) -> tuple[str | None, str | None]:
        src = self.media.get_video(vid)
        rel = None
        if src:
            prefix = f"media/v_{vid[:2]}/{vid}"
            hit = self._cached_media(prefix, ("mp4",))
            if hit:
                rel = hit
            else:
                rel = f"{prefix}.mp4"
                dst = self.out_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    import shutil
                    shutil.copyfile(src, dst)
                except Exception:
                    rel = None
        thumb = self.media.get_thumb(vid)
        trel = None
        if thumb:
            data, ext = thumb
            prefix = f"media/v_{vid[:2]}/{vid}_thumb"
            trel = self._cached_media(prefix, (ext.lstrip("."),)) or f"{prefix}.{ext}"
            d2 = self.out_dir / trel
            if not d2.exists():
                d2.parent.mkdir(parents=True, exist_ok=True)
                try:
                    d2.write_bytes(data)
                except Exception:
                    trel = None
        return rel, trel

    # ---------------------------------------------------------------- 渲染
    def render_message(self, m: dict) -> str:
        t = m["type"]
        if t == T_SYS:
            return f'<div class="sys">{_esc(m["text"])}</div>'

        if t == T_IMAGE:
            rel = self._save_image(m["img_md5"]) if m["img_md5"] else None
            if rel:
                self.stats["图片"] += 1
                return f'<a href="{rel}" target="_blank"><img class="media" src="{rel}" alt="图片" loading="lazy"></a>'
            self.stats["图片缺失"] += 1
            return '<div class="plain">[图片 · 本地无缓存]</div>'

        if t == T_VIDEO:
            rel, trel = self._save_video(m["video_id"]) if m["video_id"] else (None, None)
            sec = m.get("video_sec", 0)
            if rel:
                self.stats["视频"] += 1
                poster = f' poster="{trel}"' if trel else ""
                return (f'<video src="{rel}"{poster} controls preload="none"></video>'
                        f'<div class="plain">视频 {sec}s</div>')
            if trel:
                self.stats["视频缺失"] += 1
                return f'<img class="media" src="{trel}" alt="视频封面"><div class="plain">[视频 {sec}s · 本地无源文件]</div>'
            self.stats["视频缺失"] += 1
            return f'<div class="plain">[视频 {sec}s · 本地无缓存]</div>'

        if t == T_VOICE:
            self.stats["语音"] += 1
            ms = m.get("voice_ms", 0)
            sec = round(ms / 1000) if ms else 0
            txt = m.get("voice_text")
            inner = ""
            src = self.voice_map.get(m.get("_ts"))
            if src:
                self.stats["语音可播"] += 1
                inner = (f'<audio controls preload="none" src="{src}" style="max-width:260px"></audio>'
                         f'<div class="voice"><span class="sec">语音 {sec}″</span></div>')
            else:
                inner = f'<div class="voice"><span class="bar"></span><span class="sec">{sec}″</span></div>'
            if txt:
                inner += f'<div class="voice"><div class="txt">转文字：{_esc(txt)}</div></div>'
            elif not src:
                inner += '<div class="voice"><div class="txt" style="color:#aaa">（本地无音频缓存）</div></div>'
            return inner

        if t == T_EMOJI:
            self.stats["表情"] += 1
            name = (m.get("text") or "").split(".")[-1]
            return f'<div class="plain">[表情 {_esc(name)}]</div>'

        if t == T_APPMSG:
            self.stats["文件"] += 1
            title = m.get("title") or m.get("file_name") or "分享"
            sub = m.get("file_name") or ""
            if sub == title:
                sub = ""
            url = m.get("url")
            t_html = f'<a href="{_esc(url)}" target="_blank">{_esc(title)}</a>' if url else _esc(title)
            return (f'<div class="card"><div class="icon">文件</div><div>'
                    f'<div class="t">{t_html}</div>'
                    + (f'<div class="s">{_esc(sub)}</div>' if sub else '')
                    + '</div></div>')

        # 文本及其他
        text = m.get("text") or ""
        return _esc(text) if text else '<span style="color:#bbb">（空消息）</span>'

    # ---------------------------------------------------------------- 主流程
    def export(self, rows, sender_names: dict[int, str], self_id: int,
               title: str, subtitle: str = "") -> str:
        """rows: 已排序的消息行（含 local_type/message_content/packed_info_data/create_time/real_sender_id）"""
        parts: list[str] = []
        last_day = ""
        n = 0
        for r in rows:
            d = dict(r)
            ts = d.get("create_time") or 0
            day = _fmt_day(ts)
            if day != last_day:
                parts.append(f'<div class="day"><span>{_esc(day)}</span></div>')
                last_day = day
            m = parse_message(d.get("local_type", 0), d.get("message_content"), d.get("packed_info_data"))
            m["_ts"] = int(ts or 0)
            sid = d.get("real_sender_id")
            is_self = sid == self_id
            nm = sender_names.get(sid, str(sid))
            initial = (nm[:1] or "?") if nm else "?"
            body = self.render_message(m)
            parts.append(
                f'<div class="row{" me" if is_self else ""}">'
                f'<div class="avatar">{_esc(initial)}</div>'
                f'<div class="col"><div class="name">{_esc(nm)} · {_esc(_fmt_time(ts)[11:16])}</div>'
                f'<div class="bubble">{body}</div></div></div>'
            )
            n += 1

        html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)} · 聊天记录</title>
<style>{CSS}</style></head>
<body><div class="wrap">
<header><h1>{_esc(title)}</h1>
<div class="meta">{_esc(subtitle)}</div></header>
{''.join(parts)}
<footer>墨溯 · 共 {n} 条消息 · 生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}</footer>
</div><script>{JS}</script></body></html>"""
        out = self.out_dir / "index.html"
        out.write_text(html_doc, encoding="utf-8")
        return str(out)
