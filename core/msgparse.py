"""
墨溯 · 微信 4.x 消息体解析

实测环境：微信 Windows 4.1.15.13

核心事实（踩坑得来，勿改）：
  1. 消息体 message_content 是 **zstd 压缩的 protobuf/XML**（魔数 28 B5 2F FD），
     不再是 3.x 时代的裸 XML。纯文本消息(type=1)仍是明文。
  2. local_type 高位是 flag，**真实类型 = local_type & 0xFFFFFFFF**
     （例如 25769803825 的真实类型是 49）。
  3. 本地文件定位靠 packed_info_data 里的 id，**不是** XML 里的 md5：
       - 图片: field3 -> field4  (32 位 hex md5)
       - 视频: field4 -> field8  (32 位 ASCII id)
       - 语音: field5 -> field2  (UTF-8 转文字，音频本体不落本地)
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

try:
    import zstandard
except ImportError:  # pragma: no cover
    zstandard = None

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

# 真实消息类型（local_type & 0xFFFFFFFF）
T_TEXT = 1
T_IMAGE = 3
T_VOICE = 34
T_VIDEO = 43
T_EMOJI = 47
T_APPMSG = 49
T_SYS = 10000

TYPE_NAMES = {
    T_TEXT: "文本",
    T_IMAGE: "图片",
    T_VOICE: "语音",
    T_VIDEO: "视频",
    T_EMOJI: "表情",
    T_APPMSG: "分享/文件",
    T_SYS: "系统消息",
}


# --------------------------------------------------------------------------
# 极简 protobuf 解析（只依赖标准库）
# --------------------------------------------------------------------------
def _read_varint(b: bytes, i: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        x = b[i]
        i += 1
        result |= (x & 0x7F) << shift
        if not x & 0x80:
            return result, i
        shift += 7
        if shift > 63:
            raise ValueError("varint 过长")


def parse_pb(data: bytes) -> dict[int, list]:
    """解析 protobuf 为 {field_no: [values]}，value 为 int 或 bytes。"""
    out: dict[int, list] = {}
    i = 0
    n = len(data)
    while i < n:
        try:
            key, i = _read_varint(data, i)
        except (IndexError, ValueError):
            break
        field_no, wire = key >> 3, key & 7
        try:
            if wire == 0:
                v, i = _read_varint(data, i)
                out.setdefault(field_no, []).append(v)
            elif wire == 2:
                ln, i = _read_varint(data, i)
                out.setdefault(field_no, []).append(data[i : i + ln])
                i += ln
            elif wire == 5:
                out.setdefault(field_no, []).append(data[i : i + 4])
                i += 4
            elif wire == 1:
                out.setdefault(field_no, []).append(data[i : i + 8])
                i += 8
            else:
                break  # 未知 wire type，停止
        except (IndexError, ValueError):
            break
    return out


# 群聊文本原文以「发送者ID:」独占一行开头（微信 4.x 实测格式），正文从第二行起。
# 限制：首行 ≤64 字符、半角冒号结尾、必须紧跟换行——三者同时满足才剥，
# 用户自己打「备注:\nxxx」这类内容的误伤概率可以忽略。
_SENDER_PREFIX = re.compile(r"^.{1,64}:\r?\n")


def strip_sender_prefix(text: str) -> str:
    if text and "\n" in text:
        m = _SENDER_PREFIX.match(text)
        if m and not m.group(0).lstrip().lower().startswith(("http://", "https://")):
            return text[m.end():]
    return text


def decompress(blob: bytes, max_size: int = 8 << 20) -> bytes | None:
    """zstd 解压；非 zstd 数据原样返回。"""
    if not isinstance(blob, (bytes, bytearray)):
        return None
    blob = bytes(blob)
    if blob[:4] != ZSTD_MAGIC:
        return blob
    if zstandard is None:
        return None
    try:
        return zstandard.ZstdDecompressor().decompress(blob, max_output_size=max_size)
    except Exception:
        return None


# --------------------------------------------------------------------------
# packed_info_data -> 媒体定位信息
# --------------------------------------------------------------------------
_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")


def parse_packed(blob) -> dict:
    """从 packed_info_data 提取媒体定位信息。"""
    if not isinstance(blob, (bytes, bytearray)) or not blob:
        return {}
    info: dict = {}
    top = parse_pb(bytes(blob))

    def _sub(field: int, sub_field: int) -> list[str]:
        """取 top.field 内嵌消息的 sub_field，只保留 bytes 载荷。"""
        out = []
        for b in top.get(field, []):
            if not isinstance(b, (bytes, bytearray)):
                continue
            for s in parse_pb(bytes(b)).get(sub_field, []):
                if isinstance(s, (bytes, bytearray)) and s:
                    out.append(bytes(s).decode("latin1"))
        return out

    # 图片：field3 内嵌 field4 = 32 位 hex md5
    for t in _sub(3, 4):
        if _HEX32.match(t):
            info["img_md5"] = t.lower()
            break

    # 视频：field4 内嵌 field8 = 32 位 ASCII id
    for t in _sub(4, 8):
        if len(t) == 32:
            info["video_id"] = t
            break

    # 语音转文字：field5 内嵌 field2
    for t in _sub(5, 2):
        txt = t.encode("latin1").decode("utf-8", "ignore").strip()
        if txt:
            info["voice_text"] = txt
            break

    # 文件类（appmsg）可能带 md5
    if "img_md5" not in info:
        for t in _sub(3, 1):
            if _HEX32.match(t):
                info["file_md5"] = t.lower()
                break
    return info


# --------------------------------------------------------------------------
# 消息体 -> 结构化
# --------------------------------------------------------------------------
def _xml_attr(xml: str, tag: str, attr: str) -> str | None:
    m = re.search(r"<%s\b[^>]*\b%s=\"([^\"]*)\"" % (tag, attr), xml)
    return m.group(1) if m else None


def parse_message(local_type: int, content, packed=None) -> dict:
    """把一行消息解析成结构化 dict。"""
    real = local_type & 0xFFFFFFFF
    msg: dict = {
        "type": real,
        "type_name": TYPE_NAMES.get(real, f"未知({real})"),
        "text": "",
        "img_md5": None,
        "video_id": None,
        "voice_text": None,
        "voice_ms": 0,
        "emoji_md5": None,
        "title": None,
        "url": None,
        "file_name": None,
    }

    pinfo = parse_packed(packed)
    msg["img_md5"] = pinfo.get("img_md5")
    msg["video_id"] = pinfo.get("video_id")
    msg["voice_text"] = pinfo.get("voice_text")

    raw = content
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "ignore")
    body = decompress(raw) if isinstance(raw, (bytes, bytearray)) else None
    xml = body.decode("utf-8", "ignore") if body else ""

    if real == T_TEXT:
        msg["text"] = (body.decode("utf-8", "ignore") if body else (content or ""))
        if isinstance(msg["text"], bytes):
            msg["text"] = msg["text"].decode("utf-8", "ignore")
        msg["text"] = strip_sender_prefix(msg["text"])

    elif real == T_IMAGE:
        msg["img_md5"] = pinfo.get("img_md5")
        if xml:
            msg["file_name"] = _xml_attr(xml, "img", "md5") or None

    elif real == T_VOICE:
        if xml:
            v = _xml_attr(xml, "voicemsg", "voicelength")
            if v and v.isdigit():
                msg["voice_ms"] = int(v)
            msg["voice_format"] = _xml_attr(xml, "voicemsg", "voiceformat")

    elif real == T_VIDEO:
        msg["video_id"] = pinfo.get("video_id")
        if xml:
            pl = _xml_attr(xml, "videomsg", "playlength")
            msg["video_sec"] = int(pl) if pl and pl.isdigit() else 0

    elif real == T_EMOJI:
        if xml:
            msg["emoji_md5"] = _xml_attr(xml, "emoji", "md5")
            msg["text"] = _xml_attr(xml, "emoji", "productid") or ""

    elif real == T_APPMSG:
        if xml:
            try:
                root = ET.fromstring(xml[xml.find("<msg>") : xml.rfind("</msg>") + 6])
                app = root.find("appmsg")
                if app is not None:
                    t = app.findtext("title")
                    msg["title"] = t
                    msg["text"] = t or ""
                    msg["url"] = app.findtext("url")
                    at = app.find(".//appattach")
                    if at is not None:
                        msg["file_name"] = at.findtext("filename") or at.findtext("fileext")
                        msg["app_type"] = app.findtext("type")
                    else:
                        msg["app_type"] = app.findtext("type")
            except Exception:
                msg["text"] = _xml_attr(xml, "appmsg", "title") or ""
            # 红包/转账这类没有被 <appmsg><title> 包裹的消息，
            # 上面两种取法都会拿到空串，于是界面显示一条空白气泡。
            # 退回统一的「压成一句人话」，抽不出就至少别是空的。
            if not msg["text"]:
                msg["text"] = _readable_from_xml(xml)

    elif real == T_SYS:
        txt = xml or (content if isinstance(content, str) else "")
        # 系统消息常见是 XML（如撤回 <sysmsg type="revokemsg">），
        # 直接显示原始 XML 对人是噪音——翻译成人话
        if "<sysmsg" in txt:
            try:
                # 切片长度必须和 "</sysmsg>" 一致（9 字符）。原来写 +8 会少一个
                # '>'，ET.fromstring 每次都抛 ParseError，于是**所有撤回消息
                # 都退化成「系统消息」**——一个静默失效，没有任何报错。
                root = ET.fromstring(txt[txt.find("<sysmsg"): txt.rfind("</sysmsg>") + 9])
                stype = root.get("type") or ""
                if stype == "revokemsg":
                    repl = root.findtext(".//replacemsg") or ""
                    msg["text"] = repl or "撤回了一条消息"
                else:
                    msg["text"] = stype or "系统消息"
            except Exception:
                msg["text"] = "系统消息"
        elif "<" in txt:
            # 不是 <sysmsg> 但仍是 XML：红包领取提示（<img src="SystemMessages_
            # HongbaoIcon.png"> 你领取了…）、转账、群公告等都属于这一类。
            # 以前这里把原始 XML 原样返回，界面里就是一片标签。
            msg["text"] = _readable_from_xml(txt)
        else:
            msg["text"] = txt

    else:
        # 兜底分支以前直接吐 XML 前 200 字，界面里就是一片
        # `<voipmsg type="VoIPBubbleMsg">…<roomkey>0</roomkey>…`，
        # 既看不懂、又把 roomid/token 这类内部字段露出来。
        # 这些类型其实都带一句人能读的文本，优先把它抽出来。
        msg["text"] = _readable_from_xml(xml) if xml else (
            content.decode("utf-8", "ignore") if isinstance(content, (bytes, bytearray)) else str(content or "")
        )

    return msg


def _readable_from_xml(xml: str) -> str:
    """把「非文本类」消息的 XML 压成一句人话。

    抽不出可读内容时返回「[xxx]」形式的占位，
    绝不把原始 XML 丢到界面上（信息噪音 + 内部字段泄露）。
    """
    if not xml:
        return "[不支持的消息]"
    s = xml.strip()
    # 语音/视频通话记录：正文在 <msg><![CDATA[通话时长 00:37]]></msg>
    if s.startswith("<voipmsg"):
        m = re.search(r"<msg>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</msg>", s, re.S)
        if m:
            t = m.group(1).strip()
            if t:
                return f"[通话] {t}"
        return "[通话]"
    # 位置消息：优先 poiname（地点名），poiname 为空时退回 label（详细地址）
    if s.startswith("<?xml") or s.startswith("<msg"):
        for pat in (r'poiname="([^"]*)"', r'label="([^"]*)"'):
            m = re.search(pat, s)
            if m and m.group(1).strip():
                return f"[位置] {m.group(1).strip()}"
        if "<location" in s:
            return "[位置]"
    # 红包 / 转账 / 名片等「图标 + 一句话」的消息
    if "<img" in s:
        t = re.sub(r"<[^>]+>", "", s)
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            return t
    # 其它：抽标签之间的纯文本；若只剩标签说明没内容
    txt = re.sub(r"<[^>]+>", " ", s)
    txt = re.sub(r"\s+", " ", txt).strip()
    if txt and not txt.startswith("?xml"):
        return txt[:200]
    tag = re.match(r"<\??([\w]+)", s)
    return f"[{tag.group(1)}]" if tag else "[不支持的消息]"
