"""墨溯 MCP Server（stdio，JSON-RPC 2.0 / 换行分隔）。

把 Dataset / Analytics / Moments / Exporter 的能力以 MCP tools 暴露出来，
供 Claude Code、WorkBuddy 等支持 MCP 的客户端直接调用。

协议要点：
  - 传输：stdin/stdout 一行一个 JSON，**任何日志只能走 stderr**
  - 方法：initialize / notifications/initialized / tools/list / tools/call / ping
  - resources/list、prompts/list 返回空集合（兼容爱探测的客户端）
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.dataset import Dataset, parse_time  # noqa: E402

SERVER_INFO = {"name": "mosu", "version": "0.1.0"}
PROTOCOL = "2025-06-18"

MAX_LIMIT = 200
TEXT_CUT = 400


def log(msg: str) -> None:
    print(f"[mosu-mcp] {msg}", file=sys.stderr, flush=True)


def _cut(v, n: int = TEXT_CUT):
    if isinstance(v, str) and len(v) > n:
        return v[:n] + f"…(+{len(v)-n})"
    return v


def _ts_text(ts) -> str:
    try:
        ts = int(ts)
    except Exception:
        return ""
    if ts <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _slim_message(m: dict, cut: int = TEXT_CUT) -> dict:
    """消息瘦身：丢掉 AI 基本用不上的字段，长文本截断。"""
    keep = {
        "id": m.get("localId"),
        "time": _ts_text(m.get("createTime")),
        "direction": m.get("direction"),
        "senderName": m.get("senderName"),
        "kind": m.get("kind"),
        "content": _cut(m.get("content"), cut),
        "imgMd5": m.get("imgMd5"),
        "voiceMs": m.get("voiceMs"),
        "voiceText": m.get("voiceText"),
        "title": m.get("title"),
        "url": m.get("url"),
        "fileName": m.get("fileName"),
    }
    return {k: v for k, v in keep.items() if v not in (None, "", 0)}


# ------------------------------------------------------------------ tools
def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


TOOLS: list[dict] = [
    {
        "name": "mosu_status",
        "description": "查看墨溯数据源状态：账号目录、密钥校验、会话数/联系人数、已解密库列表。"
                       "排查任何问题先跑它。",
        "inputSchema": _schema({}),
    },
    {
        "name": "mosu_sessions",
        "description": "列出微信会话（按最后活跃时间倒序）。返回 sessionId/displayName/消息数/类型/最后活跃时间/最后一条摘要。"
                       "拿 displayName 或 sessionId 去喂 mosu_messages。",
        "inputSchema": _schema({
            "query": {"type": "string", "description": "按名字/wxid 模糊过滤，可选"},
            "limit": {"type": "integer", "description": f"条数，默认 20，最大 {MAX_LIMIT}"},
            "offset": {"type": "integer", "description": "分页偏移，默认 0"},
            "account": {"type": "string", "description": "账号名（多账号时临时指定），见 mosu account"},
        }),
    },
    {
        "name": "mosu_messages",
        "description": "读取某个会话的消息。默认返回最新的 N 条（按时间正序）。"
                       "kind 取值 text/image/voice/video/file/emoji/link/card/system/other。",
        "inputSchema": _schema({
            "session": {"type": "string", "description": "会话名（备注/昵称/群名）或 wxid，必填"},
            "limit": {"type": "integer", "description": f"条数，默认 20，最大 {MAX_LIMIT}"},
            "offset": {"type": "integer", "description": "分页偏移（0=最新一页）"},
            "type": {"type": "string", "description": "只看某类消息，如 image / voice / text"},
            "direction": {"type": "string", "enum": ["in", "out"], "description": "in=对方发来 out=我发出"},
            "from": {"type": "string", "description": "起始时间，如 2026-01-01 或 2026-01-01 10:00"},
            "to": {"type": "string", "description": "结束时间"},
            "order": {"type": "string", "enum": ["desc", "asc"], "description": "desc=取最新（默认）"},
        }, ["session"]),
    },
    {
        "name": "mosu_contacts",
        "description": "列出联系人（默认按消息量倒序，只含有名字的好友）。",
        "inputSchema": _schema({
            "query": {"type": "string", "description": "按备注/昵称/wxid 过滤"},
            "limit": {"type": "integer", "description": "默认 20"},
            "include_all": {"type": "boolean", "description": "含群聊与无昵称条目，默认 false"},
        }),
    },
    {
        "name": "mosu_contact_info",
        "description": "单个联系人的详情：备注/昵称/wxid/类型/签名/所在群/消息数。",
        "inputSchema": _schema({"name": {"type": "string", "description": "备注、昵称或 wxid"}}, ["name"]),
    },
    {
        "name": "mosu_search",
        "description": "全库关键词搜索（会逐条解压消息体，慢但准）。不指定 session 就搜全部会话。",
        "inputSchema": _schema({
            "keyword": {"type": "string", "description": "关键词，大小写不敏感"},
            "session": {"type": "string", "description": "限定在某个会话内，可选"},
            "limit": {"type": "integer", "description": "返回条数，默认 20"},
        }, ["keyword"]),
    },
    {
        "name": "mosu_stats",
        "description": "统计分析。kind: global 全局概览 / contacts 联系榜 / time 时间分布 / "
                       "session 单人统计 / keywords 关键词 / group 群成员榜。",
        "inputSchema": _schema({
            "kind": {"type": "string",
                     "enum": ["global", "contacts", "time", "session", "keywords", "group"]},
            "session": {"type": "string", "description": "kind=session/keywords/group 时的目标（名字或wxid）"},
            "by": {"type": "string", "enum": ["hour", "weekday", "day", "month", "year"],
                   "description": "kind=time 的维度，默认 month"},
            "top": {"type": "integer", "description": "榜单条数，默认 20"},
        }, ["kind"]),
    },
    {
        "name": "mosu_moments",
        "description": "读取朋友圈：作者、时间、正文、图片数、点赞与评论。",
        "inputSchema": _schema({
            "limit": {"type": "integer", "description": "默认 10"},
            "user": {"type": "string", "description": "只看某人（wxid），可选"},
            "from": {"type": "string", "description": "起始时间"},
            "to": {"type": "string", "description": "结束时间"},
        }),
    },
    {
        "name": "mosu_report",
        "description": "生成年度/全时间/单会话报告：消息量、发出收到、活跃天数、联系榜、关键词、时段分布。",
        "inputSchema": _schema({
            "year": {"type": "integer", "description": "如 2026；不给则按当前年"},
            "session": {"type": "string", "description": "只统计某个会话，可选"},
            "all_time": {"type": "boolean", "description": "全时间而非单年，默认 false"},
            "top_contacts": {"type": "integer", "description": "联系榜条数，默认 10"},
            "top_keywords": {"type": "integer", "description": "关键词条数，默认 20"},
        }),
    },
    {
        "name": "mosu_export",
        "description": "导出聊天记录。format: html（气泡式，含图片/视频）/ json / csv / txt。"
                       "返回产出文件路径。",
        "inputSchema": _schema({
            "session": {"type": "string", "description": "会话名或 wxid"},
            "format": {"type": "string", "enum": ["html", "json", "csv", "txt"], "description": "默认 html"},
            "output": {"type": "string", "description": "输出目录或文件路径；默认 <墨溯>/export"},
            "with_media": {"type": "boolean", "description": "顺带把语音转成 mp3（慢），默认 false"},
            "all": {"type": "boolean", "description": "导出全部会话（慎用，很慢），默认 false"},
            "max_sessions": {"type": "integer", "description": "配合 all 限制会话数"},
            "incremental": {"type": "boolean",
                            "description": "增量导出：只处理上次导出后的新消息（txt/csv 追加、"
                                           "json 合并、HTML 全量重渲染但媒体走磁盘缓存，大幅提速）"},
        }),
    },
    {
        "name": "mosu_profile",
        "description": "画像蒸馏。不传 session = 全库个人画像（作息/社交图谱/话题/表达风格/"
                       "月度活跃/综合侧写）；传 session = 该联系人的深度互动画像"
                       "（发起与回复/话题对比/情感曲线/亲密度信号/里程碑/关键事件时间线）。"
                       "纯本地统计，不调用外部 LLM。",
        "inputSchema": _schema({
            "session": {"type": "string", "description": "联系人/群名；不传则蒸馏全库个人画像"},
            "from": {"type": "string", "description": "起始时间，如 2026-01-01"},
            "to": {"type": "string", "description": "结束时间"},
            "top": {"type": "integer", "description": "榜单条数，默认 15"},
            "max_msgs": {"type": "integer", "description": "全库画像的文本采样上限，默认 60000"},
            "per_session": {"type": "integer", "description": "全库画像每会话采样上限，默认 300"},
            "markdown": {"type": "boolean", "description": "true 时返回渲染好的 Markdown 报告"},
        }),
    },
    {
        "name": "mosu_media",
        "description": "按图片 md5（消息的 imgMd5 字段）取出本地加密图片并解密。"
                       "默认落盘返回路径；as=base64 时直接把图回传，可直接看图。",
        "inputSchema": _schema({
            "md5": {"type": "string", "description": "32 位图片 md5"},
            "as": {"type": "string", "enum": ["path", "base64"], "description": "默认 path"},
        }, ["md5"]),
    },
]


class McpServer:
    def __init__(self, data_dir=None, key_hex: str | None = None, account: str | None = None):
        self.data_dir = data_dir
        self.key_hex = key_hex
        self._account = account
        self._ds: Dataset | None = None
        self._an = None
        self._media = None
        self._moments = None

    # ---------------------------------------------------------- 懒加载
    def _switch_account(self, name):
        """工具参数里带 account 时切换账号（重建全部缓存）。"""
        if name and name != self._account:
            self._account = name
            self._ds = self._an = self._media = self._moments = None
            log(f"切换到账号 {name}")

    @property
    def ds(self) -> Dataset:
        if self._ds is None:
            t0 = time.time()
            self._ds = Dataset(self.data_dir, key_hex=self.key_hex, quiet=True,
                               account=self._account)
            log(f"数据源就绪（账号 {self._ds.account_name}）{time.time()-t0:.1f}s")
        return self._ds

    def analytics(self):
        if self._an is None:
            from core.analytics import Analytics
            self._an = Analytics(self.ds)
        return self._an

    def media(self):
        if self._media is None:
            from core.media import MediaStore
            t0 = time.time()
            self._media = MediaStore(self.ds.user_dir, ROOT / ".cache").build()
            log(f"媒体索引 {self._media.stats()} {time.time()-t0:.1f}s")
        return self._media

    def moments(self):
        if self._moments is None:
            from core.sns import Moments
            self._moments = Moments(self.ds)
        return self._moments

    # ---------------------------------------------------------- 工具实现
    def call(self, name: str, args: dict | None = None):
        args = args or {}
        self._switch_account(args.get("account"))
        fn = getattr(self, "t_" + name, None)
        if fn is None:
            raise ValueError(f"未知工具 {name}")
        return fn(args)

    @staticmethod
    def _lim(args, default: int = 20) -> int:
        v = int(args.get("limit") or default)
        return max(1, min(v, MAX_LIMIT))

    # ---- status
    def t_mosu_status(self, a):
        ds = self.ds
        files = []
        for rel in ("session/session.db", "contact/contact.db", "message/message_0.db",
                    "message/message_1.db", "message/media_0.db", "sns/sns.db"):
            if ds.has(rel):
                files.append(rel)
        return {
            "userDir": str(ds.user_dir),
            "selfWxid": ds.self_username(),
            "keyOk": True,
            "dbFiles": files,
            "sessions": len(ds.sessions()),
            "contacts": len(ds.contacts()),
            "shards": ds.shards(),
        }

    # ---- sessions
    def t_mosu_sessions(self, a):
        ds = self.ds
        limit, offset = self._lim(a, 20), int(a.get("offset") or 0)
        ss = ds.sessions()
        q = (a.get("query") or "").strip().lower()
        if q:
            ss = [s for s in ss if q in (s["displayName"] or "").lower()
                  or q in (s["sessionId"] or "").lower()]
        total = len(ss)
        page = ss[offset:offset + limit]
        return {
            "total": total, "limit": limit, "offset": offset,
            "hasMore": total > offset + len(page),
            "sessions": [{
                "sessionId": s["sessionId"],
                "displayName": s["displayName"],
                "type": s["type"],
                "messageCount": s["messageCount"],
                "lastTime": _ts_text(s.get("lastTime")),
                "lastMessage": _cut(s.get("lastMessage"), 80),
                "unread": s.get("unread"),
            } for s in page],
        }

    # ---- messages
    def t_mosu_messages(self, a):
        ds = self.ds
        r = ds.messages(
            a["session"],
            limit=self._lim(a, 20),
            offset=int(a.get("offset") or 0),
            from_ts=parse_time(a.get("from")),
            to_ts=parse_time(a.get("to")),
            type_=a.get("type"),
            direction=a.get("direction"),
            order=a.get("order") or "desc",
        )
        return {
            "session": r["session"],
            "count": len(r["messages"]),
            "cursor": r["cursor"],
            "messages": [_slim_message(m) for m in r["messages"]],
        }

    # ---- contacts
    def t_mosu_contacts(self, a):
        ds = self.ds
        q = (a.get("query") or "").strip().lower()
        cons = list(ds.contacts().values())
        if q:
            cons = [c for c in cons if q in (c.get("remark") or "").lower()
                    or q in (c.get("nickname") or "").lower()
                    or q in (c["wxid"] or "").lower()]
        else:
            counts = {s["sessionId"]: s["messageCount"] for s in ds.sessions()}
            cons.sort(key=lambda c: counts.get(c["wxid"], 0), reverse=True)
        if not a.get("include_all"):
            cons = [c for c in cons if (c.get("remark") or c.get("nickname") or "").strip()
                    and not c["wxid"].endswith("@chatroom")]
        total = len(cons)
        page = cons[:self._lim(a, 20)]
        return {"total": total, "contacts": [{
            "wxid": c["wxid"], "remark": c.get("remark"), "nickname": c.get("nickname"),
            "type": ds.contact_type(c["wxid"]),
        } for c in page]}

    def t_mosu_contact_info(self, a):
        ds = self.ds
        name = a["name"]
        cs = ds.contacts()
        c = cs.get(name)
        if c is None:
            for v in cs.values():
                if name in ((v.get("remark") or "", v.get("nickname") or "")):
                    c = v
                    break
        if c is None:
            raise ValueError(f"没找到联系人「{name}」")
        sess = ds.find_session(c["wxid"])
        return {
            "wxid": c["wxid"], "remark": c.get("remark"), "nickname": c.get("nickname"),
            "type": ds.contact_type(c["wxid"]),
            "signature": c.get("signature"),
            "session": ({"sessionId": sess["sessionId"], "displayName": sess["displayName"],
                         "messageCount": sess["messageCount"]} if sess else None),
        }

    # ---- search
    def t_mosu_search(self, a):
        ds = self.ds
        r = ds.search(a["keyword"], session=a.get("session"), limit=self._lim(a, 20))
        return {"keyword": a["keyword"], "total": r["total"], "count": len(r["messages"]),
                "messages": [_slim_message(m) for m in r["messages"]]}

    # ---- stats
    def t_mosu_stats(self, a):
        an = self.analytics()
        kind = a["kind"]
        if kind == "global":
            return {"kind": kind, **an.global_stats()}
        if kind == "contacts":
            return {"kind": kind, **an.contact_stats(int(a.get("top") or 20))}
        if kind == "time":
            return {"kind": kind, "by": a.get("by") or "month",
                    **an.time_stats(a.get("by") or "month", session=a.get("session"))}
        if kind == "session":
            return {"kind": kind, **an.session_stats(a["session"])}
        if kind == "keywords":
            return {"kind": kind, **an.keyword_stats(a.get("session"), int(a.get("top") or 30))}
        if kind == "group":
            return {"kind": kind, **an.group_stats(a["session"])}
        raise ValueError(f"未知 stats kind: {kind}")

    # ---- moments
    def t_mosu_moments(self, a):
        r = self.moments().entries(limit=self._lim(a, 10), user=a.get("user"),
                                   from_ts=parse_time(a.get("from")),
                                   to_ts=parse_time(a.get("to")))
        out = []
        for e in r["entries"]:
            out.append({
                "createTime": e["createTime"],
                "author": e["author"].get("displayName"),
                "text": _cut(e.get("contentText"), 300),
                "mediaCount": e.get("mediaCount"),
                "mediaUrls": (e.get("mediaUrls") or [])[:6],
                "likes": e.get("likes"),
                "comments": e.get("comments"),
                "commentList": (e.get("commentList") or [])[:5],
            })
        return {"total": r["total"], "count": len(out), "entries": out}

    # ---- report
    def t_mosu_report(self, a):
        r = self.analytics().report(year=a.get("year"), all_time=bool(a.get("all_time")),
                                    session=a.get("session"),
                                    top_contacts=int(a.get("top_contacts") or 10),
                                    top_keywords=int(a.get("top_keywords") or 20))
        return r

    # ---- export
    def t_mosu_export(self, a):
        fmt = (a.get("format") or "html").lower()
        inc = bool(a.get("incremental"))
        out_root = Path(a["output"]) if a.get("output") else ROOT / "export"
        ds = self.ds
        if a.get("all"):
            targets = ds.sessions()
            if a.get("max_sessions"):
                targets = targets[:int(a["max_sessions"])]
        else:
            s = ds.find_session(a.get("session") or "")
            if not s:
                raise ValueError(f"没找到会话「{a.get('session')}」")
            targets = [s]
        results = []
        t0 = time.time()
        for sess in targets:
            if fmt == "html":
                from core import export_state as es
                out_dir = out_root / sess["displayName"] if len(targets) > 1 else out_root
                st = es.load_state(out_dir) if inc else None
                if st:
                    newest = ds.raw_messages(sess, limit=1, order="desc")
                    last = newest[0][1]["create_time"] if newest else 0
                    if last <= (st.get("lastExportTime") or 0):
                        results.append({"session": sess["displayName"], "path": str(out_dir),
                                        "count": 0, "added": 0, "skipped": True})
                        continue
                results.append(self._export_html(sess, out_root, bool(a.get("with_media")),
                                                 multi=len(targets) > 1))
            else:
                results.append(self._export_textual(sess, out_root, fmt,
                                                    multi=len(targets) > 1, incremental=inc))
        return {"format": fmt, "outputDir": str(out_root), "count": len(results),
                "messages": sum(r.get("count", 0) for r in results),
                "cost": round(time.time() - t0, 1), "results": results}

    def _safe_dirname(self, s: str) -> str:
        bad = '<>:"/\\|?*'
        return "".join(c for c in s if c not in bad).strip() or "chat"

    def _export_html(self, sess, out_root: Path, with_media: bool, multi: bool):
        from core.media import MediaStore
        from core.exporter import ChatExporter
        from core.voice import VoiceStore
        ds = self.ds
        name = self._safe_dirname(sess["displayName"])
        out_dir = out_root / name if multi else out_root
        out_dir.mkdir(parents=True, exist_ok=True)
        # 跨分片收集
        base_of = {sh["shard"]: (i + 1) * 10_000_000 for i, sh in enumerate(ds._shard_list(sess))}
        rows, names_map = [], {}
        for shard, r in ds.iter_session_rows(sess):
            d = dict(r)
            sid = d.get("real_sender_id")
            if sid == ds.self_id(shard):
                gid = 0
                names_map[0] = ds.display_name(ds.self_username())
            else:
                gid = base_of.get(shard, 0) + (sid or 0)
                names_map.setdefault(gid, ds.sender_names(shard).get(sid) or str(sid))
            d["real_sender_id"] = gid
            rows.append(d)
        rows.sort(key=lambda d: (d.get("create_time") or 0, d.get("sort_seq") or 0))
        voice_map = {}
        if with_media:
            try:
                m0 = ds.plain("message/media_0.db")
                voice_map = VoiceStore(m0).prepare(rows, out_dir, ROOT / "scripts" / "silk_batch.cjs")
            except Exception as e:
                log(f"语音跳过: {str(e)[:80]}")
        media = self.media()
        exp = ChatExporter(media, out_dir, voice_map=voice_map)
        path = exp.export(rows, sender_names=names_map, self_id=0, title=sess["displayName"],
                          subtitle=f"{sess['sessionId']} · 共 {len(rows)} 条 · {ds.user_dir.name}")
        return {"session": sess["displayName"], "path": str(path), "count": len(rows),
                "media": dict(exp.stats)}

    def _export_textual(self, sess, out_root: Path, fmt: str, multi: bool,
                        incremental: bool = False):
        from core import export_state as es
        ds = self.ds
        st = es.load_state(out_root) if incremental else None
        frm = (st.get("lastExportTime") or 0) + 1 if st else None
        r = ds.messages(sess, limit=10 ** 9, order="asc", from_ts=frm)
        if incremental and st and not r["messages"]:
            return {"session": sess["displayName"], "path": str(out_root), "count": 0,
                    "added": 0, "skipped": True}
        msgs = r["messages"]
        name = self._safe_dirname(sess["displayName"])
        if multi:
            out_root.mkdir(parents=True, exist_ok=True)
            out_file = out_root / f"{name}.{fmt}"
        else:
            out_file = out_root if str(out_root).lower().endswith(f".{fmt}") else out_root / f"{name}.{fmt}"
        out_file.parent.mkdir(parents=True, exist_ok=True)
        added = len(msgs)
        if fmt == "json":
            if incremental and st:
                out, added = es.merge_json_file(out_file, r)
                msgs = out["messages"]
            else:
                out_file.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        elif fmt == "csv":
            rows = [[_ts_text(m.get("createTime")), m["direction"],
                     m.get("senderName") or "", m["kind"],
                     (m.get("content") or "").replace("\n", " ")] for m in msgs]
            if incremental and st:
                added = es.append_csv(out_file, rows)
            else:
                import csv as _csv
                with open(out_file, "w", newline="", encoding="utf-8-sig") as f:
                    w = _csv.writer(f)
                    w.writerow(["时间", "方向", "发送者", "类型", "内容"])
                    w.writerows(rows)
        else:
            lines = []
            for m in msgs:
                who = "我" if m["direction"] == "out" else (m.get("senderName") or "对方")
                c = m.get("content") or ""
                if m["kind"] == "image":
                    c = "[图片]" + (f" md5={m.get('imgMd5')}" if m.get("imgMd5") else "")
                elif m["kind"] == "voice":
                    c = f"[语音 {m.get('voiceMs')}ms] {m.get('voiceText') or ''}"
                elif m["kind"] == "video":
                    c = "[视频]"
                elif m["kind"] == "file":
                    c = f"[文件] {m.get('title') or m.get('fileName') or ''}"
                lines.append(f"[{_ts_text(m.get('createTime'))}] {who}: {c}")
            if incremental and st:
                added = es.append_text(out_file, lines)
            else:
                out_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if msgs:
            last = max((m.get("createTime") or 0) for m in msgs)
            prev = (st or {}).get("count", 0) if incremental else 0
            es.save_state(out_file.parent, sess["sessionId"], last, prev + added)
        return {"session": sess["displayName"], "path": str(out_file),
                "count": len(msgs), "added": added}

    # ---- profile
    def t_mosu_profile(self, a):
        from core.profile import ProfileDistiller
        pd = ProfileDistiller(self.ds)
        frm, to = parse_time(a.get("from")), parse_time(a.get("to"))
        if a.get("session"):
            d = pd.contact_profile(a["session"], from_ts=frm, to_ts=to,
                                   top=int(a.get("top") or 10))
        else:
            d = pd.self_profile(from_ts=frm, to_ts=to,
                                max_msgs=int(a.get("max_msgs") or 60000),
                                top=int(a.get("top") or 15),
                                per_session=int(a.get("per_session") or 300))
        if a.get("markdown"):
            return {"markdown": pd.render_markdown(d)}
        return d

    # ---- media
    def t_mosu_media(self, a):
        md5 = (a.get("md5") or "").strip()
        if len(md5) != 32:
            raise ValueError("md5 必须是 32 位")
        pair = self.media().get_image(md5)
        if not pair:
            raise ValueError(f"本地找不到该图片（md5={md5}），可能已被清理或还没下载")
        data, ext = pair
        if (a.get("as") or "path") == "base64":
            from core.exporter import ChatExporter
            if len(data) > 900_000:
                data = ChatExporter._maybe_resize(data, ext, max_width=1024)
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                    "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp"}.get(
                        ext.lstrip(".").lower(), "image/jpeg")
            return {"_image": True, "data": base64.b64encode(data).decode(), "mime": mime,
                    "bytes": len(data)}
        out_dir = ROOT / ".cache" / "media_out"
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / f"{md5}.{ext.lstrip('.')}"
        p.write_bytes(data)
        return {"path": str(p), "bytes": len(data), "ext": ext}


# ------------------------------------------------------------------ 协议层
class Protocol:
    def __init__(self, server: McpServer):
        self.srv = server

    def handle(self, msg: dict) -> dict | None:
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        if method == "initialize":
            return self._ok(mid, {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": (
                    "墨溯 mosu：本地微信聊天数据只读接口。"
                    "先用 mosu_sessions / mosu_contacts 定位会话名，再取消息；"
                    "图片用 mosu_media(md5, as=base64) 直接看。"
                ),
            })
        if method == "notifications/initialized" or (method or "").startswith("notifications/"):
            return None
        if method == "ping":
            return self._ok(mid, {})
        if method == "tools/list":
            return self._ok(mid, {"tools": TOOLS})
        if method == "resources/list":
            return self._ok(mid, {"resources": []})
        if method == "resources/templates/list":
            return self._ok(mid, {"resourceTemplates": []})
        if method == "prompts/list":
            return self._ok(mid, {"prompts": []})
        if method == "tools/call":
            return self._call_tool(mid, params)
        if mid is None:
            return None
        return self._err(mid, -32601, f"不支持的方法: {method}")

    def _call_tool(self, mid, params):
        name = params.get("name")
        args = params.get("arguments") or {}
        t0 = time.time()
        try:
            result = self.srv.call(name, args)
        except SystemExit as e:
            return self._err(mid, -32000, str(e) or "已退出")
        except ValueError as e:
            return self._err(mid, -32602, str(e))
        except Exception as e:  # noqa: BLE001
            log(f"tool {name} 失败: {type(e).__name__}: {e}")
            return self._err(mid, -32000, f"{type(e).__name__}: {e}")
        log(f"tool {name} {time.time()-t0:.1f}s")
        content = []
        if isinstance(result, dict) and result.get("_image"):
            content.append({"type": "image", "data": result["data"],
                            "mimeType": result["mime"]})
            result = {k: v for k, v in result.items() if k not in ("_image", "data")}
            content.append({"type": "text", "text": json.dumps(result, ensure_ascii=False)})
        else:
            content.append({"type": "text",
                            "text": json.dumps(result, ensure_ascii=False, default=str)})
        out = {"content": content, "isError": False}
        if isinstance(result, dict):
            out["structuredContent"] = result
        return self._ok(mid, out)

    @staticmethod
    def _ok(mid, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _err(mid, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": code, "message": message}}


def run_stdio(data_dir=None, key_hex: str | None = None) -> int:
    """stdin/stdout 循环。返回退出码。"""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        except Exception:
            pass
    srv = McpServer(data_dir, key_hex)
    proto = Protocol(srv)
    log("MCP server 启动（stdio）")
    inp, out = sys.stdin.buffer, sys.stdout.buffer
    for raw in inp:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            log(f"坏 JSON: {e}")
            continue
        try:
            resp = proto.handle(msg)
        except Exception as e:  # noqa: BLE001
            log(f"handle 崩溃: {type(e).__name__}: {e}")
            resp = Protocol._err(msg.get("id"), -32000, f"{type(e).__name__}: {e}")
        if resp is None:
            continue
        out.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
        out.flush()
    return 0


def self_test() -> int:
    """不启动服务，直接把每个工具跑一遍（冒烟测试）。"""
    srv = McpServer()
    proto = Protocol(srv)
    checks = [
        ("initialize", {"name": "x"}),
    ]
    r = proto.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    print("initialize →", r["result"]["serverInfo"], r["result"]["protocolVersion"])
    r = proto.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    print("tools:", len(r["result"]["tools"]))
    # 用数据里真实存在的第一个会话名，避免写死任何个人信息
    probe = srv.t_mosu_sessions({"limit": 1})
    demo = (probe.get("sessions") or [{}])[0].get("displayName") or ""
    if not demo:
        print("  [!] 没有会话数据，跳过数据类工具自测")
        return 0
    for name, args in [
        ("mosu_status", {}),
        ("mosu_sessions", {"limit": 3}),
        ("mosu_messages", {"session": demo, "limit": 3}),
        ("mosu_contacts", {"limit": 3}),
        ("mosu_stats", {"kind": "global"}),
        ("mosu_stats", {"kind": "session", "session": demo}),
        ("mosu_moments", {"limit": 2}),
        ("mosu_search", {"keyword": "的", "session": demo, "limit": 3}),
    ]:
        r = proto.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": name, "arguments": args}})
        if "error" in r:
            print(f"  [x] {name}: {r['error']['message'][:120]}")
        else:
            txt = r["result"]["content"][0]["text"]
            print(f"  [✓] {name}: {len(txt)}B  {txt[:110]}…")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(self_test())
    sys.exit(run_stdio())
