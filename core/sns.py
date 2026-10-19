# -*- coding: utf-8 -*-
"""
墨溯 core —— 朋友圈（sns.db）

微信 4.1 的 SnsTimeLine 表：tid / user_name / content(XML) / pack_info_buf
"""
from __future__ import annotations

import re
import sqlite3
import time
import xml.etree.ElementTree as ET

from core.msgparse import decompress


def _xml_text(node) -> str:
    return (node.text or "") if node is not None else ""


def _first_text(node) -> str:
    """取节点文本；为空时退到首个子节点（微信有时把地址包一层）。"""
    if node is None:
        return ""
    t = (node.text or "").strip()
    if t:
        return t
    for sub in node:
        t = (sub.text or "").strip()
        if t:
            return t
    return ""


def _parse_media(m) -> dict:
    """解析单个 <media> 节点。

    微信 4.x 的图片地址必须带 token：token 是 <url>/<thumb> 上的属性，
    裸地址直接丢给浏览器一定拉不到。这里把原图地址（…/0）、缩略图地址
    （…/150）、token 与原始尺寸一起带出去，由服务端带 token 抓取并落盘缓存。
    """
    url_node = m.find("url")
    thumb_node = m.find("thumb")
    size = m.find("size")
    return {
        "md5": (url_node.get("md5") if url_node is not None else "") or "",
        "key": (url_node.get("key") if url_node is not None else "") or "",
        "token": (url_node.get("token") if url_node is not None else "") or "",
        "url": _first_text(url_node),
        "thumbUrl": _first_text(thumb_node),
        "thumbToken": (thumb_node.get("token") if thumb_node is not None else "") or "",
        "width": int(size.get("width") or 0) if size is not None else 0,
        "height": int(size.get("height") or 0) if size is not None else 0,
        "bytes": int(size.get("totalSize") or 0) if size is not None else 0,
    }


class Moments:
    def __init__(self, ds):
        self.ds = ds

    def _con(self) -> sqlite3.Connection | None:
        if not self.ds.has("sns/sns.db"):
            return None
        try:
            return self.ds.conn("sns/sns.db")
        except Exception:
            return None

    @staticmethod
    def parse(content) -> dict:
        if isinstance(content, (bytes, bytearray)):
            raw = bytes(content)
            if raw[:4] == b"\x28\xb5\x2f\xfd":
                raw = decompress(raw) or b""
            xml = raw.decode("utf-8", "ignore")
        else:
            xml = str(content or "")
        xml = xml.strip()
        if not xml:
            return {}
        if not xml.startswith("<"):
            i = xml.find("<")
            xml = xml[i:] if i >= 0 else ""
        if not xml:
            return {}
        try:
            root = ET.fromstring(xml)
        except Exception:
            return {"rawText": xml[:200]}
        tl = root.find(".//TimelineObject") if root.tag != "TimelineObject" else root
        if tl is None:
            tl = root

        def find_text(tag):
            n = tl.find(tag)
            return _xml_text(n).strip()

        create_time = find_text("createTime")
        entry = {
            "id": find_text("id"),
            "username": find_text("username"),
            "createTime": int(create_time) if create_time.isdigit() else None,
            "contentText": find_text("contentDesc"),
            "mediaUrls": [],
            "mediaMd5": [],
            "mediaItems": [],
            "mediaCount": 0,
            "likes": 0,
            "likeUsers": [],
            "comments": 0,
            "commentList": [],
        }
        media = root.find(".//mediaList")
        if media is not None:
            for m in media.findall("media"):
                etype = _xml_text(m.find("type")).strip()
                item = _parse_media(m)
                entry["mediaItems"].append(item)
                if item.get("md5"):
                    entry.setdefault("mediaMd5", []).append(item["md5"])
                if item.get("url"):
                    entry["mediaUrls"].append(item["url"])
                entry["mediaCount"] += 1
                entry.setdefault("mediaTypes", []).append(etype)
        like = root.find(".//likeList")
        if like is not None:
            entry["likeUsers"] = [_xml_text(x.find("username")).strip()
                                  for x in like.findall("like")]
            entry["likes"] = len(entry["likeUsers"])
        cmt = root.find(".//commentList")
        if cmt is not None:
            for c in cmt.findall("comment"):
                entry["commentList"].append({
                    "username": _xml_text(c.find("username")).strip(),
                    "content": _xml_text(c.find("content")).strip(),
                })
            entry["comments"] = len(entry["commentList"])
        return entry

    def _interactions(self) -> dict[str, dict]:
        """点赞/评论来自 SnsMessage_tmp3（微信 4.1 不把 likeList 写进正文 XML）。"""
        if getattr(self, "_inter_cache", None) is not None:
            return self._inter_cache
        out: dict[str, dict] = {}
        con = self._con()
        if con is None:
            self._inter_cache = out
            return out
        try:
            rows = con.execute(
                "SELECT feed_id, from_username, from_nickname, content, create_time, type "
                "FROM SnsMessage_tmp3").fetchall()
        except Exception:
            self._inter_cache = out
            return out
        for r in rows:
            key = str(r["feed_id"])
            e = out.setdefault(key, {"likes": 0, "likeUsers": [], "comments": 0, "commentList": []})
            content = (r["content"] or "").strip()
            who = r["from_username"] or ""
            name = (r["from_nickname"] or "").strip() or self.ds.display_name(who) if who else ""
            if content:
                e["comments"] += 1
                e["commentList"].append({"username": who, "displayName": name,
                                         "content": content, "createTime": r["create_time"]})
            else:
                e["likes"] += 1
                if who:
                    e["likeUsers"].append(who)
        self._inter_cache = out
        return out

    def entries(self, limit: int = 20, user: str | None = None,
                from_ts=None, to_ts=None, offset: int = 0) -> dict:
        con = self._con()
        if con is None:
            return {"entries": [], "total": 0, "limit": limit,
                    "meta": {"nativeSupported": False, "note": "未找到 sns/sns.db"}}
        t0 = time.time()
        rows = []
        for r in con.execute("SELECT tid, user_name, content FROM SnsTimeLine"):
            c = r["content"]
            e = self.parse(c)
            if not e:
                continue
            ct = e.get("createTime") or 0
            author = e.get("username") or r["user_name"]
            if user and user not in (author, r["user_name"]):
                continue
            if from_ts and ct < from_ts:
                continue
            if to_ts and ct > to_ts:
                continue
            rows.append({
                "id": e.get("id") or str(r["tid"]),
                "tid": str(r["tid"]),
                "author": {"wxid": author, "displayName": self.ds.display_name(author)},
                "createTime": ct,
                "contentText": e.get("contentText", ""),
                "mediaCount": e.get("mediaCount", 0),
                "mediaUrls": e.get("mediaUrls", []),
                "mediaMd5": e.get("mediaMd5", []),
                "mediaItems": e.get("mediaItems", []),
                "likes": e.get("likes", 0),
                "likeUsers": e.get("likeUsers", []),
                "comments": e.get("comments", 0),
                "commentList": e.get("commentList", []),
            })
        inter = self._interactions()
        for e in rows:
            it = inter.get(e["tid"]) or inter.get(e["id"])
            if it:
                e["likes"] = e["likes"] or it["likes"]
                e["likeUsers"] = e["likeUsers"] or it["likeUsers"]
                e["comments"] = e["comments"] or it["comments"]
                e["commentList"] = e["commentList"] or it["commentList"]
        rows.sort(key=lambda x: x["createTime"], reverse=True)
        total = len(rows)
        # offset：本方法本就全表解析后内存排序切片，offset 零额外成本；
        # GUI「加载更多」分页用（CLI 不传 offset，行为不变）
        return {"entries": rows[offset:offset + limit], "total": total,
                "limit": limit, "offset": offset,
                "meta": {"nativeSupported": True,
                         "cost": round(time.time() - t0, 2)}}
