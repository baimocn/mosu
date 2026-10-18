# -*- coding: utf-8 -*-
"""
墨溯 core —— 朋友圈（sns.db）

微信 4.1 的 SnsTimeLine 表：tid / user_name / content(XML) / pack_info_buf
"""
from __future__ import annotations

import re
import sqlite3
import xml.etree.ElementTree as ET

from core.msgparse import decompress


def _xml_text(node) -> str:
    return (node.text or "") if node is not None else ""


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
                urls, md5s = [], []
                for u in m.findall("url"):
                    t = (u.text or "").strip()
                    if not t:
                        for sub in u:
                            t = (sub.text or "").strip()
                            if t:
                                break
                    if t:
                        urls.append(t)
                        md5s.append(u.get("md5") or "")
                        break
                if urls:
                    entry["mediaUrls"].append(urls[0])
                    if md5s[0]:
                        entry.setdefault("mediaMd5", []).append(md5s[0])
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
                from_ts=None, to_ts=None) -> dict:
        con = self._con()
        if con is None:
            return {"entries": [], "total": 0, "limit": limit,
                    "meta": {"nativeSupported": False, "note": "未找到 sns/sns.db"}}
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
        return {"entries": rows[:limit], "total": total, "limit": limit,
                "meta": {"nativeSupported": True}}
