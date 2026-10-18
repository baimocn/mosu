#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
墨溯 CLI —— 微信本地数据命令行工具
全自有实现：无 dll、无授权检查、无网络请求。

用法:
    python cli/mosu.py status
    python cli/mosu.py sessions --limit 20
    python cli/mosu.py messages "张三" --limit 50 --type image
    python cli/mosu.py search 关键词 --limit 20
    python cli/mosu.py stats global | contacts | time | session <名> | keywords <名> | group <群>
    python cli/mosu.py export "张三"              # HTML（含图片/语音/视频）
    python cli/mosu.py export --all --output out    # 全量导出
    python cli/mosu.py moments --limit 10
    python cli/mosu.py report --year 2026
    python cli/mosu.py contacts | contacts info <名>
    python cli/mosu.py key get | test | set <hex>

全局选项:
    --db-path <dir>  微信账号目录（或 db_storage 的上一级）
    --key <hex>      手动指定 64 位数据库密钥
    --format <fmt>   json | jsonl | table | csv | markdown
    --limit <n>      条数限制
    --quiet          不打印过程日志
    --refresh        忽略缓存，重新解密
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.dataset import (  # noqa: E402
    Dataset, KEYVAULT, LEGACY_VAULT, load_keyvault, load_key_hex, find_user_dir,
    parse_time, save_keyvault, load_accounts, resolve_account, save_accounts,
)
from core.formatters import emit, fmt_time  # noqa: E402


# ---------------------------------------------------------------- 工具
def save_config(patch: dict) -> dict:
    """写入墨溯自己的密钥库（~/.mosu/config.json）。"""
    data = load_keyvault()
    data.update(patch)
    save_keyvault(data)
    return data


def not_implemented(name: str, note: str = "") -> None:
    print(json.dumps({"error": "NOT_IMPLEMENTED", "command": name, "note": note},
                     ensure_ascii=False, indent=2))


def count_db_files(ds: Dataset) -> int:
    n = 0
    for p in ds.db_root.rglob("*.db"):
        if p.name.endswith(("-wal", "-shm")):
            continue
        n += 1
    return n


# ---------------------------------------------------------------- status
def cmd_status(args, ds: Dataset):
    from core.sqlcipher4 import verify_password
    from core.dataset import load_accounts
    accounts, current = load_accounts()
    info = {
        "configured": bool(ds.key_hex),
        "configPath": str(KEYVAULT),
        "account": ds.account_name,
        "accountCount": len(accounts),
        "dataDir": str(ds.user_dir),
        "dbPath": str(ds.db_root),
        "wxid": ds.self_username(),
        "databaseFiles": count_db_files(ds),
        "cacheDir": str(ROOT / ".cache"),
    }
    conn = {"attempted": True, "ok": False}
    try:
        src = ds.db_root / "session/session.db"
        ok, detail = verify_password(ds.key, str(src))
        conn["keyValid"] = ok
        if ok:
            n = len(ds.sessions())
            conn.update({"ok": True, "sessionCount": n,
                         "contactCount": len(ds.contacts())})
        else:
            conn["error"] = "密钥 HMAC 校验失败"
    except Exception as e:
        conn["error"] = str(e)[:120]
    info["connection"] = conn
    if args.format == "table":
        print("\n  墨溯 · 运行状态")
        print("  " + "-" * 60)
        print(f"  配置文件 : {info['configPath']}  ({'已配置' if info['configured'] else '未配置'})")
        print(f"  数据目录 : {info['dataDir']}")
        print(f"  数据库   : {info['dbPath']}（{info['databaseFiles']} 个 .db）")
        print(f"  本机 wxid: {info['wxid']}")
        print(f"  密钥校验 : {'✅ 通过' if conn.get('keyValid') else '❌ 失败'}")
        if conn.get("ok"):
            print(f"  会话数   : {conn['sessionCount']}   联系人: {conn['contactCount']}")
        elif conn.get("error"):
            print(f"  连接错误 : {conn['error']}")
        print()
    else:
        emit(info, args.format)


# ---------------------------------------------------------------- init / config
def cmd_init(args, ds: Dataset):
    patch = {}
    if args.db_path:
        patch["dbPath"] = str(args.db_path)
    if args.key:
        patch["keyHex"] = args.key.strip()
    if args.wxid:
        patch["wxid"] = args.wxid
    data = save_config(patch) if patch else load_keyvault()
    if args.format == "table":
        print("\n  墨溯 · 初始化")
        print(f"  配置文件 : {KEYVAULT}")
        print(f"  数据目录 : {ds.user_dir}")
        print(f"  密钥     : {'已写入' if patch.get('keyHex') else ('已存在' if data.get('keyHex') else '未配置')}")
        print("\n  下一步: mosu status  →  mosu sessions\n")
    else:
        emit({"configPath": str(KEYVAULT), "dataDir": str(ds.user_dir),
              "configured": bool(data.get("keyHex"))}, args.format)


def cmd_config(args, ds: Dataset):
    if args.config_cmd == "show":
        data = load_keyvault()
        safe = dict(data)
        if safe.get("keyHex"):
            k = safe["keyHex"]
            safe["keyHex"] = k[:8] + "*" * 48 + k[-8:]
        if args.format == "table":
            print(f"\n  配置文件: {KEYVAULT}\n")
            for k, v in safe.items():
                print(f"  {k:<16} {v}")
            print()
        else:
            emit(safe, args.format)
        return
    if args.config_cmd == "set":
        patch = {}
        if args.db_path:
            patch["dbPath"] = str(args.db_path)
        if args.key:
            patch["keyHex"] = args.key.strip()
        if args.wxid:
            patch["wxid"] = args.wxid
        if args.default_limit:
            patch["defaultLimit"] = args.default_limit
        if not patch:
            print("[!] 没有要设置的内容，例如 --db-path <dir>")
            return
        data = save_config(patch)
        emit({"saved": True, "config": {k: data.get(k) for k in patch}}, args.format)
        return
    not_implemented("config", "子命令缺失")


# ---------------------------------------------------------------- key
def cmd_key(args, ds: Dataset):
    if args.key_cmd == "get":
        k = ds.key_hex
        shown = k if args.full else k[:8] + "*" * 48 + k[-8:]
        src = KEYVAULT if KEYVAULT.exists() else (
            LEGACY_VAULT if LEGACY_VAULT.exists() else KEYVAULT)
        if args.format == "table":
            print(f"\n  当前密钥: {shown}\n  来源: {src}\n")
        else:
            emit({"keyHex": shown, "source": str(src)}, args.format)
        return
    if args.key_cmd == "test":
        from core.sqlcipher4 import verify_password
        src = ds.db_root / "session/session.db"
        ok, detail = verify_password(ds.key, str(src))
        out = {"validFormat": bool(re.fullmatch(r"[0-9a-fA-F]{64}", ds.key_hex)),
               "hmacOk": ok, "db": str(src), "salt": detail.get("salt")}
        if args.format == "table":
            print(f"\n  格式: {'✅ 64 位 hex' if out['validFormat'] else '❌ 不是 64 位 hex'}")
            print(f"  HMAC: {'✅ 通过（可解密）' if ok else '❌ 失败（密钥不匹配）'}")
            print(f"  库  : {src}\n")
        else:
            emit(out, args.format, raw=out)
        return
    if args.key_cmd == "set":
        k = args.hex.strip().lower()
        if not re.fullmatch(r"[0-9a-fA-F]{64}", k):
            sys.exit("[x] 密钥必须是 64 位十六进制字符串")
        save_config({"keyHex": k})
        from core.sqlcipher4 import verify_password
        src = ds.db_root / "session/session.db" if ds.db_root.exists() else None
        ok = verify_password(bytes.fromhex(k), str(src))[0] if src else None
        emit({"saved": True, "keyHex": k[:8] + "*" * 48 + k[-8:], "hmacOk": ok}, args.format)
        return
    if args.key_cmd == "migrate":
        # 把历史遗留的旧版密钥库迁到墨溯自己的库
        if not LEGACY_VAULT.exists():
            emit({"migrated": False, "note": f"没有发现旧密钥库 {LEGACY_VAULT}"}, args.format)
            return
        legacy = json.loads(LEGACY_VAULT.read_text(encoding="utf-8"))
        current = load_keyvault() if KEYVAULT.exists() else {}
        moved, skipped = [], []
        for k, v in legacy.items():
            if k in current and current[k] != v:
                skipped.append(k)          # 新库已有不同值，不覆盖
            else:
                current[k] = v
                moved.append(k)
        save_keyvault(current)
        out = {"migrated": True, "from": str(LEGACY_VAULT), "to": str(KEYVAULT),
               "movedKeys": moved, "skippedKeys": skipped,
               "note": "旧库未删除，确认墨溯工作正常后可手动删除"}
        emit(out, args.format, raw=out)
        return
    not_implemented("key", "可用: get | test | set | migrate")


# ---------------------------------------------------------------- account（多账号）
def cmd_account(args, ds: Dataset):
    accounts, current = load_accounts()
    sub = args.account_cmd or "list"

    if sub == "list":
        if not accounts:
            print("\n  [i] 还没有任何账号，先 `mosu account add <名> --db-path <目录> --key <hex>`\n")
            return
        rows = []
        for name, acc in accounts.items():
            rows.append({
                "账号": name + (" ← 当前" if name == current else ""),
                "数据目录": str(acc.get("dbPath") or "—"),
                "密钥": (acc.get("keyHex") or "")[:8] + "…" if acc.get("keyHex") else "—",
                "图片密钥": "有" if acc.get("imgKeyHex") else "—",
            })
        if args.format == "table":
            print(f"\n  共 {len(accounts)} 个账号（配置 {KEYVAULT}）\n")
            for r in rows:
                print(f"  {r['账号']}")
                print(f"    目录: {r['数据目录']}  密钥: {r['密钥']}  图片密钥: {r['图片密钥']}")
            print()
        else:
            emit(rows, args.format, raw={"accounts": list(accounts), "current": current,
                                         "count": len(accounts)})
        return

    if sub == "current":
        emit({"current": current, "count": len(accounts)}, args.format,
             raw={"current": current, "count": len(accounts)})
        if args.format == "table":
            print(f"\n  当前账号: {current or '（未设置）'}\n")
        return

    if sub == "add":
        name = (args.name or "").strip()
        if not name:
            sys.exit("[x] 要给账号起个名，例如 wxid_xxxx")
        db_path = getattr(args, "db_path", None)
        key = getattr(args, "key", None)
        if not db_path and not key:
            sys.exit("[x] 至少给 --db-path 或 --key 之一")
        acc = dict(accounts.get(name) or {})
        acc["wxid"] = name
        if db_path:
            acc["dbPath"] = str(db_path)
        if key:
            k = key.strip().lower()
            if not re.fullmatch(r"[0-9a-fA-F]{64}", k):
                sys.exit("[x] 密钥必须是 64 位十六进制字符串")
            acc["keyHex"] = k
        img_key = getattr(args, "img_key", None)
        if img_key:
            acc["imgKeyHex"] = img_key.strip().lower()
        import time as _t
        acc["addedAt"] = _t.strftime("%Y-%m-%d %H:%M:%S")
        accounts[name] = acc
        if len(accounts) == 1 or getattr(args, "use", False):
            current = name                     # 第一个账号自动设为当前
        save_accounts(accounts, current=current)
        emit({"saved": True, "account": name, "current": current,
              "count": len(accounts)}, args.format,
             raw={"saved": True, "account": name, "current": current})
        if args.format == "table":
            print(f"\n  [✓] 账号「{name}」已保存"
                  f"（{'已设为当前' if current == name else f'当前仍是 {current}'}）\n")
        return

    if sub == "use":
        name = (args.name or "").strip()
        if name not in accounts:
            sys.exit(f"[x] 没有账号「{name}」，现有：{', '.join(accounts) or '（空）'}")
        save_accounts(accounts, current=name)
        emit({"current": name}, args.format, raw={"current": name})
        if args.format == "table":
            print(f"\n  [✓] 已切换到「{name}」\n")
        return

    if sub == "remove":
        name = (args.name or "").strip()
        if name not in accounts:
            sys.exit(f"[x] 没有账号「{name}」")
        del accounts[name]
        if current == name:
            current = next(iter(accounts), None)
        save_accounts(accounts, current=current)
        emit({"removed": name, "current": current, "count": len(accounts)}, args.format,
             raw={"removed": name, "current": current})
        if args.format == "table":
            print(f"\n  [✓] 已删除「{name}」，当前账号：{current or '（无）'}\n")
        return

    not_implemented("account", "可用: list | current | add | use | remove")


# ---------------------------------------------------------------- sessions
def cmd_sessions(args, ds: Dataset):
    ss = ds.sessions()
    if args.type:
        ss = [s for s in ss if s["type"] == args.type]
    total = len(ss)
    page = ss[args.offset:args.offset + args.limit]
    rows = [{
        "显示名": s["displayName"],
        "类型": {"private": "私聊", "group": "群聊", "mp": "公众号"}.get(s["type"], s["type"]),
        "消息数": s["messageCount"],
        "最后时间": fmt_time(s["lastTime"]),
        "摘要": (s["lastMessage"] or "")[:28],
        "wxid": s["sessionId"],
    } for s in page]
    emit(rows, args.format, meta={"total": total, "limit": args.limit,
                                  "offset": args.offset, "hasMore": total > args.offset + len(page)},
         raw=page)


# ---------------------------------------------------------------- messages
def cmd_messages(args, ds: Dataset):
    r = ds.messages(args.session, limit=args.limit, offset=args.offset,
                    from_ts=parse_time(getattr(args, "from")), to_ts=parse_time(args.to),
                    type_=args.type, direction=args.direction,
                    cursor=args.cursor, order=args.order)
    msgs = r["messages"]
    if args.format in ("json", "jsonl"):
        emit(r, args.format, meta={"total": len(msgs), "session": r["session"]["displayName"]})
        return
    rows = []
    for m in msgs:
        c = m["content"]
        if m["kind"] == "image":
            c = f"[图片] {m['imgMd5'] or ''}"
        elif m["kind"] == "voice":
            c = f"[语音 {m['voiceMs']}ms] {m['voiceText'] or ''}"
        elif m["kind"] == "video":
            c = "[视频]"
        elif m["kind"] == "file":
            c = f"[文件] {m['title'] or m['fileName'] or ''}"
        elif m["kind"] == "emoji":
            c = f"[表情] {c}"
        rows.append({
            "时间": fmt_time(m["createTime"]),
            "方向": "← 收" if m["direction"] == "in" else "发 →",
            "发送者": m["senderName"] or "",
            "类型": m["kind"],
            "内容": c,
        })
    emit(rows, args.format, meta={
        "session": r["session"]["displayName"], "total": r["session"]["messageCount"],
        "count": len(rows), "cursor": r["cursor"]},
         raw=r)


# ---------------------------------------------------------------- contacts
def cmd_contacts(args, ds: Dataset):
    if args.contact_cmd == "info":
        q = args.contact
        cons = ds.contacts()
        hit = cons.get(q)
        if not hit:
            cands = [v for k, v in cons.items()
                     if q.lower() in k.lower() or q in (v.get("remark") or "") or q in (v.get("nickname") or "")]
            if not cands:
                sys.exit(f"[x] 没找到联系人「{q}」")
            hit = cands[0]
        out = {
            "wxid": hit["wxid"],
            "displayName": ds.display_name(hit["wxid"]),
            "type": ds.contact_type(hit["wxid"]),
            "remark": hit["remark"],
            "nickname": hit["nickname"],
            "alias": hit["alias"],
            "avatarUrl": hit["avatarUrl"],
        }
        sessions = ds.find_sessions(hit["wxid"])
        if sessions:
            s = sessions[0]
            out["messageCount"] = s["messageCount"]
            out["lastTime"] = fmt_time(s["lastTime"])
        emit(out, args.format)
        return

    cons = list(ds.contacts().values())
    if args.type:
        cons = [c for c in cons if ds.contact_type(c["wxid"]) == args.type]
    if not getattr(args, "all_contacts", False):
        cons = [c for c in cons if (c.get("remark") or c.get("nickname") or "").strip()
                and not c["wxid"].endswith("@chatroom")]
    counts = {s["sessionId"]: s["messageCount"] for s in ds.sessions()}
    if getattr(args, "sort", "messages") == "name":
        cons.sort(key=lambda c: (c.get("remark") or c.get("nickname") or c["wxid"]).lower())
    else:
        cons.sort(key=lambda c: (-counts.get(c["wxid"], 0),
                                 (c.get("remark") or c.get("nickname") or c["wxid"]).lower()))
    total = len(cons)
    page = cons[args.offset:args.offset + args.limit]
    rows = [{
        "备注": c["remark"],
        "昵称": c["nickname"],
        "类型": {"friend": "好友", "group": "群聊", "mp": "公众号",
                "former_friend": "已删除", "other": "其他"}.get(ds.contact_type(c["wxid"]), "其他"),
        "消息数": counts.get(c["wxid"], 0),
        "wxid": c["wxid"],
    } for c in page]
    emit(rows, args.format, meta={"total": total, "limit": args.limit}, raw=page)


# ---------------------------------------------------------------- search
def cmd_search(args, ds: Dataset):
    r = ds.search(args.keyword, session=args.session, limit=args.limit,
                  from_ts=parse_time(getattr(args, "from")), to_ts=parse_time(args.to))
    if args.format in ("json", "jsonl"):
        emit(r, args.format, meta={"total": r["total"], "keyword": args.keyword})
        return
    rows = [{
        "时间": fmt_time(m["createTime"]),
        "会话": m["sessionName"],
        "方向": "←" if m["direction"] == "in" else "→",
        "内容": (m["content"] or "")[:60],
    } for m in r["messages"]]
    emit(rows, args.format, meta={"total": r["total"], "count": len(rows),
                                  "keyword": args.keyword}, raw=r)


# ---------------------------------------------------------------- stats
def cmd_stats(args, ds: Dataset):
    from core.analytics import Analytics
    an = Analytics(ds)
    t0 = time.time()

    if args.stats_cmd == "global":
        r = an.global_stats(parse_time(getattr(args, "from")), parse_time(args.to))
        if args.format == "table":
            print("\n  全局统计")
            print("  " + "-" * 52)
            print(f"  消息总数 : {r['totalMessages']}")
            print(f"  会话数   : {r['totalSessions']}    联系人: {r['totalContacts']}")
            print(f"  文本消息 : {r['textMessages']}    媒体消息: {r['mediaMessages']}")
            print(f"  时间范围 : {r['timeRange']['firstText']} ~ {r['timeRange']['lastText']}")
            print("  类型分布 :")
            for k, v in r["byType"].items():
                print(f"      {k:<10} {v}")
            print()
        else:
            emit(r, args.format)
        return

    if args.stats_cmd == "contacts":
        r = an.contact_stats(args.top)
        rows = [{"排名": i + 1, "联系人": c["displayName"], "类型": c["type"], "消息数": c["messageCount"],
                 "wxid": c["wxid"]} for i, c in enumerate(r["contacts"])]
        emit(rows, args.format, meta={"total": r["total"], "top": args.top}, raw=r)
        return

    if args.stats_cmd == "time":
        r = an.time_stats(args.by, session=args.session)
        emit(r["items"], args.format, meta={"by": args.by, "total": r["total"]}, raw=r)
        return

    if args.stats_cmd == "session":
        r = an.session_stats(args.session)
        if args.format == "table":
            s = r["session"]
            print(f"\n  {s['displayName']}（{s['sessionId']}）")
            print("  " + "-" * 52)
            print(f"  消息总数 : {r['totalMessages']}   文本: {r['textMessages']}  媒体: {r['mediaMessages']}")
            print(f"  发出/收到: {r['sentMessages']} / {r['receivedMessages']}")
            print(f"  活跃天数 : {r['activeDays']}")
            print(f"  时间范围 : {r['firstMessageText']} ~ {r['lastMessageText']}")
            print("  类型分布 : " + "  ".join(f"{k}={v}" for k, v in r["byType"].items()))
            print()
        else:
            emit(r, args.format)
        return

    if args.stats_cmd == "keywords":
        r = an.keyword_stats(args.session, args.top)
        rows = [{"排名": i + 1, "词": k["word"], "次数": k["count"]}
                for i, k in enumerate(r["keywords"])]
        emit(rows, args.format, meta={"scanned": r["scannedMessages"], "cost": f"{r['cost']}s"},
             raw=r)
        return

    if args.stats_cmd == "group":
        r = an.group_stats(args.group)
        if args.format == "table":
            print(f"\n  {r['session']['displayName']}")
            print(f"  消息总数 : {r['totalMessages']}   活跃成员: {r['activeMembers']}")
            print("  发言榜   :")
            for i, m in enumerate(r["topMembers"][:15], 1):
                print(f"    {i:>2}. {m['displayName'][:20]:<20} {m['messageCount']}"
                      + ("  (我)" if m["isSelf"] else ""))
            print()
        else:
            emit(r, args.format)
        return

    not_implemented("stats", "可用: global | contacts | time | session | keywords | group")


# ---------------------------------------------------------------- moments
def cmd_moments(args, ds: Dataset):
    from core.sns import Moments
    r = Moments(ds).entries(limit=args.limit, user=args.user,
                            from_ts=parse_time(getattr(args, "from")), to_ts=parse_time(args.to))
    if args.format in ("json", "jsonl"):
        emit(r, args.format)
        return
    if not r["entries"]:
        print(f"\n  [i] 没有朋友圈数据（{r['meta'].get('note','')}）\n")
        return
    rows = [{
        "时间": fmt_time(e["createTime"]),
        "作者": e["author"]["displayName"],
        "正文": (e["contentText"] or "")[:40],
        "图/视频": e["mediaCount"],
        "赞": e["likes"],
        "评论": e["comments"],
    } for e in r["entries"]]
    emit(rows, args.format, meta={"total": r["total"], "limit": args.limit}, raw=r)


# ---------------------------------------------------------------- report
def cmd_report(args, ds: Dataset):
    from core.analytics import Analytics
    r = Analytics(ds).report(year=args.year, all_time=args.all_time, session=args.session,
                             top_contacts=args.top_contacts, top_keywords=args.top_keywords)
    if args.format in ("json", "jsonl"):
        emit(r, args.format)
        return
    s = r["summary"]
    scope = {"year": f"{r['year']} 年度", "all": "全时间", "session": f"会话 {r['sessionId']}"}[r["scope"]]
    print(f"\n  {scope}报告")
    print("  " + "-" * 52)
    print(f"  消息总数 : {s['totalMessages']}")
    print(f"  发出/收到: {s['sentMessages']} / {s['receivedMessages']}")
    print(f"  活跃天数 : {s['activeDays']}")
    print(f"  首条消息 : {fmt_time(s['firstMessageTime'])}")
    print(f"  末条消息 : {fmt_time(s['lastMessageTime'])}")
    print("  联系榜   :")
    for i, c in enumerate(s["topContacts"][:10], 1):
        print(f"    {i:>2}. {c['displayName'][:20]:<20} {c['count']}")
    print("  关键词   : " + "  ".join(f"{k['word']}({k['count']})" for k in s["topKeywords"][:15]))
    hours = s["hourlyDistribution"]
    if hours:
        top = sorted(hours.items(), key=lambda x: -x[1])[:5]
        print("  活跃时段 : " + "  ".join(f"{h}点({c})" for h, c in top))
    print()


# ---------------------------------------------------------------- bot（数字分身服务）
def cmd_bot(args, ds: Dataset):
    from core.bot import BotService, run_server
    corpus = Path(args.corpus) if args.corpus else ROOT / "persona" / "corpus.jsonl"
    if not corpus.exists():
        sys.exit(f"[x] 语料不存在：{corpus}\n    先跑 `mosu persona --output persona/` 生成")
    sub = args.bot_cmd or "serve"
    if sub == "serve":
        print(f"  [i] 语料 {corpus}，启动 bot 服务…", file=sys.stderr)
        return run_server(corpus, port=args.port)
    if sub == "test":
        bot = BotService(corpus)
        q = " ".join(args.query) or "在吗"
        if args.dry:
            pv = bot.preview_prompt(q)
            print(json.dumps(pv, ensure_ascii=False, indent=2))
            return
        r = bot.chat(q)
        if args.format in ("json", "jsonl"):
            emit(r, args.format)
            return
        print(f"\n  问: {q}")
        print(f"  检索参考:")
        for c in r["retrieved"]:
            print(f"    [{c['session']}] {c['text'][:50]}")
        print(f"  回复: {r['reply']}")
        print(f"  校验: {'通过' if r['passed'] else '未过 ' + str(r['problems'])}"
              f"（{r['tries']} 次尝试，{r['cost']}s）\n")
        return
    not_implemented("bot", "可用: serve | test")


# ---------------------------------------------------------------- persona（个人 bot 语料）
def cmd_persona(args, ds: Dataset):
    from core.persona import build_persona
    out = Path(args.output) if args.output else ROOT / "persona"
    print(f"  [i] 正在收集本人发言语料（全库扫描，约 1-2 分钟）…", file=sys.stderr)
    r = build_persona(ds, out)
    if args.format in ("json", "jsonl"):
        emit(r, args.format)
        return
    print(f"\n[\u2713] 个人语料包已生成 → {out}")
    print(f"    语料 {r['corpus']['corpus']:,} 条 / {r['corpus']['sessions']} 个会话")
    print(f"    平均长度 {r['profile']['avgLen']} 字")
    print(f"    风格画像: {r['styleProfile']}\n")


# ---------------------------------------------------------------- profile（画像蒸馏）
def cmd_profile(args, ds: Dataset):
    from core.profile import ProfileDistiller
    pd = ProfileDistiller(ds)
    frm = parse_time(getattr(args, "from"))
    to = parse_time(args.to)
    if args.session:
        d = pd.contact_profile(args.session, from_ts=frm, to_ts=to, top=args.top)
        title = d["session"]["displayName"]
    else:
        d = pd.self_profile(from_ts=frm, to_ts=to, max_msgs=args.max_msgs, top=args.top)
        title = ds.self_username() or "本人"
    if args.format in ("json", "jsonl"):
        emit(d, args.format)
        return
    if args.format in ("table", "csv"):
        print("  [!] 画像只支持 --format md|json，已按 markdown 输出", file=sys.stderr)
    md = pd.render_markdown(d)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(f"[\u2713] 「{title}」画像已写入 {out}", file=sys.stderr)
    else:
        print(md)


# ---------------------------------------------------------------- export
def _collect_rows(ds: Dataset, sess: dict, from_ts=None, to_ts=None):
    """跨分片收集一个会话的消息（可限定时间范围），并把 sender id 统一到同一命名空间。"""
    base_of = {sh["shard"]: (i + 1) * 10_000_000 for i, sh in enumerate(ds._shard_list(sess))}
    out_rows, names, self_ids = [], {}, set()
    for shard, r in ds.iter_session_rows(sess, from_ts, to_ts):
        d = dict(r)
        sid = d.get("real_sender_id")
        if sid == ds.self_id(shard):
            gid = 0
            self_ids.add(0)
            names[0] = ds.display_name(ds.self_username())
        else:
            gid = base_of.get(shard, 0) + (sid or 0)
            if gid not in names:
                names[gid] = ds.sender_names(shard).get(sid) or str(sid)
        d["real_sender_id"] = gid
        out_rows.append(d)
    out_rows.sort(key=lambda d: (d.get("create_time") or 0, d.get("sort_seq") or 0))
    return out_rows, names, 0


def _export_textual(ds: Dataset, sess: dict, out_file: Path, fmt: str, args,
                    incremental: bool = False):
    """文本格式导出。incremental=True 时只处理新消息：
    txt/csv 追加到文件尾，json 读旧文件合并去重后重写。"""
    from core import export_state as es
    state = es.load_state(out_file.parent) if incremental else None
    frm = parse_time(getattr(args, "from", None))
    to = parse_time(getattr(args, "to", None))
    if incremental and state:
        since = (state.get("lastExportTime") or 0) + 1
        from_ts = since if frm is None else max(frm, since)   # 增量与显式范围取更严的
    else:
        from_ts = frm
    r = ds.messages(sess, limit=10 ** 9, order="asc", from_ts=from_ts, to_ts=to)
    msgs = r["messages"]
    if incremental and state and not msgs:
        return str(out_file), 0, 0          # 没有新消息，什么都不做

    out_file.parent.mkdir(parents=True, exist_ok=True)
    added = len(msgs)
    if fmt == "json":
        if incremental:
            out, added = es.merge_json_file(out_file, r)
            msgs = out["messages"]
        else:
            out_file.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    elif fmt == "csv":
        rows = [[fmt_time(m["createTime"]),
                 "out" if m["direction"] == "out" else "in",
                 m["senderName"] or "", m["kind"],
                 (m["content"] or "").replace("\n", " ")] for m in msgs]
        if incremental:
            added = es.append_csv(out_file, rows)
        else:
            import csv as _csv
            with open(out_file, "w", newline="", encoding="utf-8-sig") as f:
                w = _csv.writer(f)
                w.writerow(["时间", "方向", "发送者", "类型", "内容"])
                w.writerows(rows)
    else:  # txt
        lines = []
        for m in msgs:
            who = "我" if m["direction"] == "out" else (m["senderName"] or "对方")
            c = m["content"] or ""
            if m["kind"] == "image":
                c = "[图片]"
            elif m["kind"] == "voice":
                c = f"[语音 {m['voiceMs']}ms] {m['voiceText'] or ''}"
            elif m["kind"] == "video":
                c = "[视频]"
            elif m["kind"] == "file":
                c = f"[文件] {m['title'] or m['fileName'] or ''}"
            lines.append(f"[{fmt_time(m['createTime'])}] {who}: {c}")
        if incremental:
            added = es.append_text(out_file, lines)
        else:
            out_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if not msgs and state:
        return str(out_file), 0, 0          # 没有新消息，什么都不做
    last = max((m["createTime"] for m in msgs), default=0) or (state or {}).get("lastExportTime", 0)
    prev = (state or {}).get("count", 0) if incremental else 0
    es.save_state(out_file.parent, sess["sessionId"], last, prev + added)
    return str(out_file), len(msgs), added


def _safe_dirname(s: str) -> str:
    bad = '<>:"/\\|?*'
    return "".join(c for c in s if c not in bad).strip() or "chat"


def cmd_export(args, ds: Dataset):
    from core.media import MediaStore
    from core.voice import VoiceStore
    from core.exporter import ChatExporter
    from core import export_state as es

    fmt = (args.export_format or "html").lower()
    inc = bool(getattr(args, "incremental", False))
    out_root = Path(args.output) if args.output else ROOT / "export"
    t_start = time.time()

    if args.all:
        targets = ds.sessions()
        if args.max_sessions:
            targets = targets[:args.max_sessions]
    else:
        s = ds.find_session(args.session or "")
        if not s:
            sys.exit(f"[x] 没找到会话「{args.session}」，先跑 mosu sessions")
        targets = [s]

    results = []
    media = None
    if fmt == "html":
        media = MediaStore(ds.user_dir, ROOT / ".cache").build()
        print(f"  [i] 媒体索引 {media.stats()}", file=sys.stderr)

    # 同名会话（不同 wxid）必须落不同目录，否则互相覆盖/屏蔽
    used_dirs: dict[str, str] = {}

    def _unique_dir(base: Path, sess: dict) -> Path:
        key = str(base)
        clash = key in used_dirs and used_dirs[key] != sess["sessionId"]
        if not clash and base.exists():
            st = es.load_state(base)
            clash = bool(st and st.get("sessionId") and st["sessionId"] != sess["sessionId"])
        if clash:
            base = base.parent / f"{base.name}_{sess['sessionId'][-6:]}"
        used_dirs[str(base)] = sess["sessionId"]
        return base

    for i, sess in enumerate(targets, 1):
        name = _safe_dirname(sess["displayName"])
        if fmt == "html":
            # 单会话：直接输出到 --output；--all：按会话名建子目录
            out_dir = out_root / name if (args.all or len(targets) > 1) else out_root
            out_dir = _unique_dir(out_dir, sess)
            out_dir.mkdir(parents=True, exist_ok=True)
            state = es.load_state(out_dir) if inc else None
            if state:
                # 增量：先看有没有新消息，没有就整个跳过（秒退）
                newest = ds.raw_messages(sess, limit=1, order="desc")
                last = newest[0][1]["create_time"] if newest else 0
                if last <= (state.get("lastExportTime") or 0):
                    print(f"  [{i}/{len(targets)}] {sess['displayName']} 无新消息，跳过"
                          f"（上次导出至 {fmt_time(state.get('lastExportTime'))}）", file=sys.stderr)
                    results.append({"path": str(out_dir / "index.html"), "count": 0,
                                    "added": 0, "session": sess["displayName"], "skipped": True})
                    continue
                print(f"  [i] 增量导出：上次至 {fmt_time(state.get('lastExportTime'))}，"
                      f"有新消息（最新 {fmt_time(last)}）", file=sys.stderr)
            # HTML 是整体文件：有新消息就全量重渲染（媒体命中磁盘缓存，很快）；
            # state 只用于上面"有无新消息"的判断，不当数据过滤器
            h_frm = parse_time(getattr(args, "from", None))
            h_to = parse_time(getattr(args, "to", None))
            rows, names, self_id = _collect_rows(ds, sess, h_frm, h_to)
            voice_map = {}
            if args.with_media:
                try:
                    m0 = ds.plain("message/media_0.db")
                    voice_map = VoiceStore(m0).prepare(rows, out_dir, ROOT / "scripts" / "silk_batch.cjs")
                except Exception as e:
                    print(f"  [!] 语音跳过: {str(e)[:60]}", file=sys.stderr)
            exp = ChatExporter(media, out_dir, voice_map=voice_map)
            path = exp.export(rows, sender_names=names, self_id=self_id, title=sess["displayName"],
                              subtitle=f"{sess['sessionId']} · 共 {len(rows)} 条 · {ds.user_dir.name}")
            added = len(rows) - (state.get("count") or 0) if state else len(rows)
            last = max((r.get("create_time") or 0) for r in rows) if rows else 0
            es.save_state(out_dir, sess["sessionId"], last, len(rows))
            results.append({"path": path, "count": len(rows), "added": max(0, added),
                            "session": sess["displayName"], "media": dict(exp.stats)})
            if not args.quiet:
                print(f"  [{i}/{len(targets)}] {sess['displayName']} → {path}"
                          f"（{len(rows)} 条{'，新增 ' + str(max(0, added)) if inc else ''}）",
                      file=sys.stderr)
        else:
            # --output 以目标后缀结尾就当文件用，否则当目录
            if args.output and args.output.lower().endswith(f".{fmt}"):
                out_file = Path(args.output)
                state_dir = out_file.parent
            else:
                out_file = out_root / f"{name}.{fmt}"
                state_dir = out_root
            path, n, added = _export_textual(ds, sess, out_file, fmt, args, incremental=inc)
            skipped = bool(inc and n == 0)
            results.append({"path": path, "count": n, "added": added,
                            "session": sess["displayName"], "skipped": skipped})
            if not args.quiet:
                if skipped:
                    print(f"  [{i}/{len(targets)}] {sess['displayName']} 无新消息，跳过",
                          file=sys.stderr)
                else:
                    tail = f"，新增 {added}" if inc else ""
                    print(f"  [{i}/{len(targets)}] {sess['displayName']} → {path}"
                          f"（{n} 条{tail}）", file=sys.stderr)

    if args.format in ("json", "jsonl"):
        emit({"exported": results, "count": len(results),
              "cost": round(time.time() - t_start, 1)}, args.format)
    else:
        total = sum(r["count"] for r in results)
        added_total = sum(r.get("added", 0) for r in results)
        skipped = sum(1 for r in results if r.get("skipped"))
        note = f"（新增 {added_total} 条，跳过 {skipped} 个无新消息会话）" if inc else ""
        print(f"\n[✓] 导出完成 {len(results)} 个会话 / {total} 条消息{note}"
              f"（{time.time()-t_start:.1f}s）")
        print(f"    目录: {out_root}\n")


# ---------------------------------------------------------------- mcp
def cmd_mcp(args, ds: Dataset):
    from core.mcp_server import run_stdio, self_test
    cmd = args.mcp_cmd or "serve"
    if cmd == "serve":
        print("  [i] MCP server（stdio）启动，日志走 stderr", file=sys.stderr)
        return run_stdio(args.db_path, key_hex=args.key)
    if cmd == "selftest":
        return self_test()
    if cmd == "config":
        import json as _j
        entry = {
            "mosu": {
                "command": sys.executable or "python",
                "args": [str(ROOT / "core" / "mcp_server.py")],
                "env": {"PYTHONIOENCODING": "utf-8"},
            }
        }
        print(_j.dumps(entry, ensure_ascii=False, indent=2))
        print(f"\n  把上面这段加进 ~/.workbuddy/mcp.json 的 mcpServers 里，"
              f"然后在连接器管理页点「信任」。", file=sys.stderr)
        return 0
    not_implemented("mcp", "可用: serve | selftest | config")


# ---------------------------------------------------------------- 入口
def build_parser():
    # 子命令也能接收的全局项（default=SUPPRESS，避免覆盖主 parser 已解析的值）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--limit", type=int, default=argparse.SUPPRESS, help="条数限制")
    common.add_argument("--format", default=argparse.SUPPRESS,
                        choices=["json", "jsonl", "table", "csv", "markdown"], help="输出格式")
    common.add_argument("--db-path", dest="db_path", default=argparse.SUPPRESS, help="微信账号目录")
    common.add_argument("--key", default=argparse.SUPPRESS, help="64 位 hex 数据库密钥")
    common.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS, help="不打印过程日志")
    common.add_argument("--wxid", default=argparse.SUPPRESS,
                        help="临时指定账号（不改默认），见 mosu account")
    common.add_argument("--refresh", action="store_true", default=argparse.SUPPRESS, help="忽略缓存重新解密")
    common.add_argument("--with-wal", action="store_true", default=argparse.SUPPRESS, help="尝试合并 WAL 帧")

    common2 = argparse.ArgumentParser(add_help=False)
    common2.add_argument("--limit", type=int, default=argparse.SUPPRESS, help="条数限制")
    common2.add_argument("--format", default=argparse.SUPPRESS,
                         choices=["json", "jsonl", "table", "csv", "markdown"], help="输出格式")
    common2.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS)

    ap = argparse.ArgumentParser(prog="mosu", description="墨溯 · 微信本地数据 CLI")
    ap.add_argument("--db-path", dest="db_path", help="微信账号目录（含 db_storage）")
    ap.add_argument("--key", help="手动指定 64 位 hex 数据库密钥")
    ap.add_argument("--wxid", help="指定微信账户 wxid（可选）")
    ap.add_argument("--format", default="table",
                    choices=["json", "jsonl", "table", "csv", "markdown"], help="输出格式")
    ap.add_argument("--limit", type=int, default=20, help="条数限制")
    ap.add_argument("--quiet", action="store_true", help="不打印过程日志")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存重新解密")
    ap.add_argument("--with-wal", action="store_true", help="尝试合并 WAL 帧（实验性）")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("status", help="检查配置与数据库连接", parents=[common])
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("init", help="初始化配置", parents=[common2])
    p.add_argument("--db-path", dest="db_path")
    p.add_argument("--key")
    p.add_argument("--wxid")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("config", help="配置管理", parents=[common2])
    p.add_argument("config_cmd", choices=["show", "set"])
    p.add_argument("--db-path", dest="db_path")
    p.add_argument("--key")
    p.add_argument("--wxid")
    p.add_argument("--default-limit", dest="default_limit", type=int)
    p.set_defaults(func=cmd_config)

    # 注意：--db-path/--key 已由 common 提供，这里不重复定义（同 option string 会冲突）
    p = sub.add_parser("account", help="多账号管理", parents=[common])
    p.add_argument("account_cmd", nargs="?",
                   choices=["list", "current", "add", "use", "remove"], default="list")
    p.add_argument("name", nargs="?")
    p.add_argument("--img-key", dest="img_key", default=argparse.SUPPRESS)
    p.add_argument("--use", action="store_true", default=argparse.SUPPRESS,
                   help="add 后直接设为当前账号")
    p.set_defaults(func=cmd_account)

    p = sub.add_parser("key", help="密钥管理", parents=[common])
    p.add_argument("key_cmd", choices=["get", "test", "set", "migrate"])
    p.add_argument("hex", nargs="?")
    p.add_argument("--full", action="store_true")
    p.set_defaults(func=cmd_key)

    p = sub.add_parser("sessions", help="列出会话", parents=[common])
    p.add_argument("--type", choices=["private", "group", "mp"])
    p.add_argument("--offset", type=int, default=0)
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("messages", help="查询会话消息", parents=[common])
    p.add_argument("session")
    p.add_argument("--from", dest="from")
    p.add_argument("--to", dest="to")
    p.add_argument("--type", choices=["text", "image", "voice", "video", "file", "emoji", "system"])
    p.add_argument("--direction", choices=["in", "out"])
    p.add_argument("--cursor")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--order", choices=["asc", "desc"], default="desc")
    p.set_defaults(func=cmd_messages)

    p = sub.add_parser("contacts", help="联系人", parents=[common])
    p.add_argument("contact_cmd", nargs="?", choices=["info"])
    p.add_argument("contact", nargs="?")
    p.add_argument("--type", choices=["friend", "group", "mp", "former_friend"])
    p.add_argument("--all", dest="all_contacts", action="store_true", help="含无昵称/群聊的全部条目")
    p.add_argument("--sort", choices=["messages", "name"], default="messages")
    p.add_argument("--offset", type=int, default=0)
    p.set_defaults(func=cmd_contacts)

    p = sub.add_parser("search", help="全文搜索", parents=[common])
    p.add_argument("keyword")
    p.add_argument("--session")
    p.add_argument("--from", dest="from")
    p.add_argument("--to", dest="to")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("stats", help="统计分析", parents=[common])
    p.add_argument("stats_cmd", choices=["global", "contacts", "time", "session", "keywords", "group"])
    p.add_argument("session", nargs="?")
    p.add_argument("group", nargs="?")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--by", choices=["hour", "weekday", "month", "date"], default="month")
    p.add_argument("--from", dest="from")
    p.add_argument("--to", dest="to")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("moments", help="朋友圈", parents=[common])
    p.add_argument("--user")
    p.add_argument("--from", dest="from")
    p.add_argument("--to", dest="to")
    p.set_defaults(func=cmd_moments)

    p = sub.add_parser("bot", help="数字分身：云端 LLM + RAG 风格锚点 + 规则硬校验",
                       parents=[common])
    p.add_argument("bot_cmd", nargs="?", choices=["serve", "test"], default="serve")
    p.add_argument("query", nargs="*", help="test 模式的问题")
    p.add_argument("--corpus", help="语料 jsonl 路径，默认 persona/corpus.jsonl")
    p.add_argument("--port", type=int, default=int(os.environ.get("MOSU_BOT_PORT", "8787")))
    p.add_argument("--dry", action="store_true", help="只看 prompt 组装，不调 LLM")
    p.set_defaults(func=cmd_bot)

    p = sub.add_parser("persona", help="个人 bot 素材：本人发言语料 + 风格画像",
                       parents=[common])
    p.add_argument("--output", help="输出目录，默认 <项目>/persona")
    p.set_defaults(func=cmd_persona)

    p = sub.add_parser("profile", help="画像蒸馏（不给会话名=全库个人画像）",
                       parents=[common])
    p.add_argument("session", nargs="?", help="联系人/群名；不给则蒸馏全库个人画像")
    p.add_argument("--from", dest="from", help="起始时间")
    p.add_argument("--to", dest="to", help="结束时间")
    p.add_argument("--top", type=int, default=15, help="榜单条数")
    p.add_argument("--max-msgs", type=int, default=30000,
                   help="全库画像扫描的消息上限（按最近活跃排序）")
    p.add_argument("--output", help="写入文件（默认打印到终端）")
    p.set_defaults(func=cmd_profile, format="markdown")

    p = sub.add_parser("report", help="年度报告", parents=[common])
    p.add_argument("--year", type=int)
    p.add_argument("--all-time", action="store_true")
    p.add_argument("--session")
    p.add_argument("--top-contacts", type=int, default=10)
    p.add_argument("--top-keywords", type=int, default=20)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("export", help="导出聊天数据", parents=[common])
    p.add_argument("session", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--output")
    p.add_argument("--from", dest="from")
    p.add_argument("--to", dest="to")
    p.add_argument("--with-media", action="store_true", help="含语音转码（较慢）")
    p.add_argument("--export-format", dest="export_format",
                   choices=["html", "json", "csv", "txt"], default="html")
    p.add_argument("--max-sessions", type=int, help="--all 时最多导出多少个会话")
    p.add_argument("--incremental", action="store_true",
                   help="增量导出：只处理上次导出之后的新消息（txt/csv 追加、"
                        "json 合并、HTML 媒体走磁盘缓存）")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("mcp", help="MCP Server（stdio）", parents=[common])
    p.add_argument("mcp_cmd", nargs="?", choices=["serve", "selftest", "config"],
                   default="serve", help="serve=起服务 selftest=自测 config=打印接入配置")
    p.set_defaults(func=cmd_mcp)

    return ap


def main():
    ap = build_parser()
    args = ap.parse_args()
    if not getattr(args, "func", None):
        ap.print_help()
        return
    if args.cmd in ("init", "config") and getattr(args, "config_cmd", "show") == "show" and args.cmd == "config":
        pass
    wxid = getattr(args, "wxid", None)
    try:
        ds = Dataset(data_dir=args.db_path, key_hex=args.key, refresh=args.refresh,
                     quiet=args.quiet, with_wal=args.with_wal, account=wxid)
    except SystemExit:
        # account/config 是配置管理命令，本身不碰数据，没配好也要能跑
        if args.cmd in ("account", "config"):
            ds = None
        else:
            raise
    args.func(args, ds)


if __name__ == "__main__":
    main()
