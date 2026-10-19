# -*- coding: utf-8 -*-
"""微信 ClawBot（iLink 协议）通道 —— 墨溯自研，不依赖 OpenClaw。

协议事实全部来自官方插件 TypeScript 源码（src/api/api.ts、src/auth/login-qr.ts、
src/messaging/inbound.ts、src/api/types.ts），不是猜的。要点：

- 端点：https://ilinkai.weixin.qq.com/ilink/bot/{getupdates,sendmessage,...}
- 每个请求头必带：AuthorizationType=ilink_bot_token、X-WECHAT-UIN（随机 uint32→base64）、
  iLink-App-Id="bot"、iLink-App-ClientVersion（版本号编码 uint32）
- 每个请求体必带 base_info: {channel_version, bot_agent}
- 长轮询 35s 超时属**正常控制流**，不是错误，直接重试
- 回复必须原样带回该消息的 context_token，否则串错对话窗口
- ⚠️ 同一 bot token 只能有一个消费者：跑本模块前必须停掉 OpenClaw 的 weixin channel

凭证/游标/上下文 token 全部存在 ~/.mosu/（MOSU_HOME 可覆盖）。
"""
from __future__ import annotations

import base64
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"          # 官方插件 package.json 里的公开常量，非私有密钥
BOT_TYPE = "3"
CHANNEL_VERSION = "1.0.0"
BOT_AGENT = "Mosu/1.0"

# 协议枚举（src/api/types.ts）
MSG_TYPE_BOT = 2              # MessageType.BOT
MSG_STATE_FINISH = 2          # MessageState.FINISH
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

LONG_POLL_TIMEOUT = 35        # 服务端 hold ~35s
QR_TTL = 300                  # 二维码 5 分钟
MAX_QR_REFRESH = 3

# 首次回复时的风格告知。进程内只发一次（_greeted 门闩），不落盘——
# 对方早就知道的事，重复发就是骚扰；重启后重新说一遍则可以接受。
PERSONA_INTRO = ("我是「倾听者」：少说多听、接住情绪，不给建议。"
                 "想换风格发 /模仿（你的口气）、/分析（用真实数据）、/安慰（先接情绪）。")
_greeted = False


class WeixinApiError(Exception):
    """协议层错误（HTTP 4xx/5xx）。

    必须与长轮询超时严格区分：超时是**正常控制流**（服务端 hold 到 35s 就断开），
    HTTP 错误是真故障（如 token 失效=401）。混淆两者会让服务"假装在跑"却永不收消息。
    """


def _client_version(v: str) -> int:
    """版本号编码成 uint32：0x00MMNNPP（major<<16 | minor<<8 | patch）。"""
    parts = [int(x) for x in v.split(".")[:3] if x.isdigit()]
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts[:3]
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


def _wechat_uin() -> str:
    """X-WECHAT-UIN：随机 uint32 的十进制字符串再 base64。每次请求都变（防重放）。"""
    return base64.b64encode(str(random.getrandbits(32)).encode()).decode()


def mosu_home() -> Path:
    return Path(os.environ.get("MOSU_HOME") or (Path.home() / ".mosu"))


def cred_path() -> Path:
    return mosu_home() / "wechat.json"


def sync_path() -> Path:
    return mosu_home() / "wechat_sync.json"


def ctx_path() -> Path:
    return mosu_home() / "wechat_ctx.json"


def _read_json(p: Path) -> dict:
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _write_json(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load_cred() -> dict:
    return _read_json(cred_path())


def save_cred(token: str, base_url: str = "", account_id: str = "",
              user_id: str = "") -> None:
    _write_json(cred_path(), {
        "token": token,
        "base_url": base_url or DEFAULT_BASE_URL,
        "account_id": account_id,
        "user_id": user_id,
        "savedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    })


def clear_cred() -> None:
    for p in (cred_path(), sync_path(), ctx_path()):
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass


class WeixinClient:
    """iLink 协议客户端（OAuth 登录 + 长轮询收发）。"""

    def __init__(self, token: str = "", base_url: str | None = None):
        self.token = token or ""
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")

    # ---- 底层 ----
    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": _wechat_uin(),
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(_client_version(CHANNEL_VERSION)),
            # 部分网关按 UA 拒默认库 UA（本机代理就拒 Python-urllib），带上无害
            "User-Agent": "Mozilla/5.0",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _base_info(self) -> dict:
        return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}

    def _post(self, endpoint: str, body: dict, timeout: int = 15):
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers=self._headers(), method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _get(self, endpoint: str, timeout: int = 15):
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    # ---- 登录 ----
    def fetch_qrcode(self) -> dict:
        """POST ilink/bot/get_bot_qrcode?bot_type=3 → {qrcode, qrcode_img_content}"""
        ep = f"ilink/bot/get_bot_qrcode?bot_type={urllib.parse.quote(BOT_TYPE)}"
        # 带上本机已有 token，服务端可据此判定"已绑定"避免重复发码
        existing = load_cred().get("token")
        body = {"local_token_list": [existing] if existing else []}
        return self._post(ep, body, timeout=15)

    def poll_qr_status(self, qrcode: str, verify_code: str = "") -> dict:
        """GET ilink/bot/get_qrcode_status?qrcode=... （长轮询）

        超时/网络错误都视为 {"status":"wait"}——长轮询超时是正常控制流。
        """
        ep = f"ilink/bot/get_qrcode_status?qrcode={urllib.parse.quote(qrcode)}"
        if verify_code:
            ep += f"&verify_code={urllib.parse.quote(verify_code)}"
        try:
            return self._get(ep, timeout=LONG_POLL_TIMEOUT)
        except urllib.error.HTTPError as e:
            raise WeixinApiError(
                f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}")
        except (urllib.error.URLError, TimeoutError, OSError):
            return {"status": "wait"}   # 长轮询超时=正常

    # ---- 收发 ----
    def get_updates(self, buf: str = "") -> tuple[list, str]:
        """长轮询收消息。返回 (msgs, 新游标)。

        - 超时/网络抖动 → ([], 原游标)，正常控制流
        - HTTP 4xx/5xx   → 抛 WeixinApiError（真故障，必须暴露）
        """
        try:
            resp = self._post(
                "ilink/bot/getupdates",
                {"get_updates_buf": buf or "", "base_info": self._base_info()},
                timeout=LONG_POLL_TIMEOUT)
        except urllib.error.HTTPError as e:
            raise WeixinApiError(
                f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}")
        except (urllib.error.URLError, TimeoutError, OSError):
            return [], buf
        msgs = resp.get("msgs") or []
        return msgs, resp.get("get_updates_buf") or buf

    def send_text(self, to_user_id: str, context_token: str, text: str) -> dict:
        msg = {
            "to_user_id": to_user_id,
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "context_token": context_token or "",
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
        return self._post("ilink/bot/sendmessage",
                          {"msg": msg, "base_info": self._base_info()}, timeout=15)

    def get_config(self, ilink_user_id: str, context_token: str = "") -> dict:
        return self._post("ilink/bot/getconfig",
                          {"ilink_user_id": ilink_user_id,
                           "context_token": context_token,
                           "base_info": self._base_info()}, timeout=10)

    def send_typing(self, ilink_user_id: str, typing_ticket: str,
                    status: int = 1) -> dict:
        """1=正在输入 2=取消。让对方立刻看到反馈，别干等云端 LLM。"""
        return self._post("ilink/bot/sendtyping",
                          {"ilink_user_id": ilink_user_id,
                           "typing_ticket": typing_ticket,
                           "status": status,
                           "base_info": self._base_info()}, timeout=10)


# ---------------------------------------------------------------------------
# 消息解析
# ---------------------------------------------------------------------------

def extract_text(msg: dict) -> str:
    """从一条消息里取出可处理的文本：文本直接取，语音取转写文字，媒体降级成占位符。"""
    items = msg.get("item_list") or []
    for it in items:
        t = it.get("type")
        if t == ITEM_TEXT:
            txt = (it.get("text_item") or {}).get("text")
            if txt:
                return str(txt)
        if t == ITEM_VOICE:
            txt = (it.get("voice_item") or {}).get("text")
            if txt:
                return str(txt)
    for it in items:
        t = it.get("type")
        if t == ITEM_IMAGE:
            return "[图片]"
        if t == ITEM_VIDEO:
            return "[视频]"
        if t == ITEM_FILE:
            return "[文件]"
        if t == ITEM_VOICE:
            return "[语音]"
    return ""


# ---------------------------------------------------------------------------
# CLI 动作
# ---------------------------------------------------------------------------

def _show_qr(url: str) -> None:
    """终端画二维码 + 给链接 + 存 PNG（IDE 里可直接扫图）。"""
    print("\n用手机微信扫描以下二维码（有效期很短，尽快扫）：\n")
    try:
        import qrcode
        q = qrcode.QRCode(box_size=1, border=1)
        q.add_data(url)
        q.make(fit=True)
        q.print_ascii(invert=True)
        png = mosu_home() / "wechat-login.png"
        qrcode.make(url).save(png)
        print(f"\n二维码图片已存：{png}  （终端看不清就打开这张图扫）")
    except Exception:
        pass
    print(f"\n若二维码不可用，打开此链接继续：\n{url}\n")


def run_login() -> int:
    """扫码登录：取码 → 展示 → 轮询直到 confirmed。"""
    client = WeixinClient()
    try:
        qr = client.fetch_qrcode()
    except Exception as e:
        print(f"[x] 获取二维码失败：{type(e).__name__}: {e}")
        return 1
    qrcode_id = qr.get("qrcode") or ""
    url = qr.get("qrcode_img_content") or ""
    if not qrcode_id:
        print(f"[x] 服务端未返回 qrcode：{qr}")
        return 1
    _show_qr(url)

    started = time.time()
    refresh = 1
    last_status = ""
    while time.time() - started < QR_TTL * (MAX_QR_REFRESH + 1):
        st = client.poll_qr_status(qrcode_id)
        status = st.get("status") or ""
        if status != last_status:
            print(f"[状态] {status}")
            last_status = status
        if status == "confirmed":
            token = st.get("bot_token") or ""
            if not token:
                print("[x] 服务端未返回 bot_token")
                return 1
            save_cred(token, st.get("baseurl") or DEFAULT_BASE_URL,
                      st.get("ilink_bot_id") or "", st.get("ilink_user_id") or "")
            print("\n✅ 已连接到微信。凭证已存 ~/.mosu/wechat.json")
            print("   下一步：mosu wechat serve")
            return 0
        if status == "expired":
            refresh += 1
            if refresh > MAX_QR_REFRESH:
                print("[x] 二维码多次失效，请稍后重试。")
                return 1
            print(f"[!] 二维码已过期，刷新第 {refresh} 张...")
            qr = client.fetch_qrcode()
            qrcode_id = qr.get("qrcode") or ""
            _show_qr(qr.get("qrcode_img_content") or "")
            continue
        if status in ("need_verifycode", "verify_code_blocked"):
            code = input("微信上显示的数字，输入后回车：").strip()
            st = client.poll_qr_status(qrcode_id, code)
            continue
        if status == "binded_redirect":
            print("✅ 该微信已绑定过，无需重复连接（沿用本机已有凭证）。")
            return 0
        time.sleep(1)
    print("[x] 登录超时。")
    return 1


def run_status() -> int:
    cred = load_cred()
    if not cred.get("token"):
        print("未登录。运行：mosu wechat login")
        return 1
    print(f"已登录：account={cred.get('account_id','?')}")
    print(f"  user={cred.get('user_id','?')}")
    print(f"  base_url={cred.get('base_url')}")
    print(f"  保存于 {cred.get('savedAt')}")
    sync = _read_json(sync_path())
    print(f"  游标={'有' if sync.get('buf') else '无（从头开始）'}")
    return 0


def run_logout() -> int:
    clear_cred()
    print("已清除微信凭证/游标/上下文 token。")
    return 0


def run_serve(corpus: str, once: bool = False) -> int:
    """长轮询收消息 → 墨溯 BotService 生成回复 → 原路发回。"""
    global _greeted
    cred = load_cred()
    if not cred.get("token"):
        print("[x] 未登录，先运行：mosu wechat login")
        return 1
    from core.bot.service import BotService

    client = WeixinClient(cred["token"], cred.get("base_url"))
    bot = BotService(corpus)
    # 分析者/安慰者要能引用真实统计：懒挂数据层，首条消息要用到时才解密
    try:
        from core.dataset import Dataset
        bot.ds = Dataset(quiet=True)
    except Exception as e:      # 没有密钥/数据目录时降级：只有不看数据的人格能用
        print(f"[i] 数据层不可用（{type(e).__name__}），分析者/安慰者不可用", flush=True)
    buf = _read_json(sync_path()).get("buf", "")
    ctx = _read_json(ctx_path())
    typing_tickets: dict[str, str] = {}   # typing_ticket 按用户缓存，免每次 getconfig
    logf = mosu_home() / "wechat.log"

    def log(msg: str) -> None:
        """常驻时没有控制台，日志必须落盘——否则出了故障无从查起。"""
        line = f"{time.strftime('%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        try:
            with open(logf, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    log(f"[mosu-wechat] 启动，长轮询中 base={client.base_url} 日志={logf}")

    fails = 0
    while True:
        try:
            msgs, buf = client.get_updates(buf)
            fails = 0
        except WeixinApiError as e:
            fails += 1
            log(f"[!] 轮询失败（第 {fails} 次）：{e}")
            if fails >= 8:
                log("[x] 连续失败过多，退出（交给计划任务拉起）")
                return 1
            time.sleep(min(2 ** fails, 60))
            continue
        _write_json(sync_path(), {"buf": buf})
        # 心跳：常驻活性证明。GUI/外部靠它判断"轮询是否真活着"——
        # 计划任务显示 Running 不代表进程没假死
        _write_json(mosu_home() / "wechat.heartbeat", {"ts": int(time.time())})
        for m in msgs:
            frm = m.get("from_user_id") or ""
            token_ctx = m.get("context_token") or ctx.get(frm, "")
            if token_ctx:
                ctx[frm] = token_ctx
            text = extract_text(m)
            if not text or not frm:
                continue
            log(f"[收到] {text}")
            # 角色切换指令：/倾听 /分析 /安慰 /模仿 /帮助
            from core.bot import personas as P
            pid, _rest = P.parse_switch(text)
            if pid == "__help__":
                try:
                    client.send_text(frm, token_ctx, P.SWITCH_HELP)
                    log("[切换] 帮助")
                except Exception as e:
                    log(f"[!] 帮助发送失败：{e}")
                continue
            if pid:
                p = bot.set_persona(pid)
                try:
                    client.send_text(frm, token_ctx,
                                     f"切到「{p.name}」了，{p.tagline}。{P.SWITCH_HELP}")
                    log(f"[切换] {p.id}")
                except Exception as e:
                    log(f"[!] 切换回执发送失败：{e}")
                continue
            # 先给"正在输入"反馈：云端 LLM 要等 3~8 秒，不能让对方干等没反应
            try:
                ticket = typing_tickets.get(frm)
                if not ticket:
                    ticket = client.get_config(frm, token_ctx).get("typing_ticket") or ""
                    if ticket:
                        typing_tickets[frm] = ticket
                if ticket:
                    client.send_typing(frm, ticket, 1)
            except Exception:
                pass      # 体验增强，失败绝不能影响主流程
            try:
                r = bot.chat(text, conv_id="wechat")
                reply = r.get("reply") or ""
                client.send_text(frm, token_ctx, reply)
                # 首次回复时补一句「我现在是谁」——微信里没有下拉框，
                # 对方只会看到一个陌生账号在说话，不知道能换风格。
                # 用模块级门闩保证进程内只发一次，不落盘。
                if not _greeted and r.get("persona") == P.DEFAULT_PERSONA:
                    try:
                        client.send_text(frm, token_ctx, PERSONA_INTRO)
                        _greeted = True
                    except Exception as e:
                        log(f"[!] 风格告知发送失败：{e}")
                _write_json(ctx_path(), ctx)
                log(f"[回复/{r.get('persona') or '-'}] {reply}")
            except Exception as e:
                log(f"[!] 处理失败：{type(e).__name__}: {e}")
        if once:
            break
    return 0


# ---------------------------------------------------------------------------
# 常驻（Windows 计划任务：登录即启动 + 崩溃自愈 + 无执行时长限制）
# ---------------------------------------------------------------------------

TASK_NAME = "MosuWechat"


def run_install() -> int:
    """注册为常驻任务（登录自启 + 崩溃自愈）。实现见 core/task.py。"""
    from core import task
    return task.install_task(TASK_NAME, "wechat serve")


def run_uninstall() -> int:
    from core import task
    return task.uninstall_task(TASK_NAME)


def run_task_status() -> int:
    from core import task
    return task.task_status(TASK_NAME)
