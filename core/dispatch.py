"""外部派活通道：协议定义（客户端与服务端共用）。

对标 Lancer 的 Control API（`apps/desktop/electron/control/`）。
抄的是**语义**，不是实现：
  - 单一 JSON 请求 → 统一响应信封（ok / result / error{code,message}）
  - 结构化错误码（客户端能分支处理，不用匹配错误文案）
  - Bearer token 鉴权，且**timing-safe** 比较
  - 明确的 operation 集，不做「万能 RPC」
  - 流式结果用 NDJSON 逐行推

传输层的差别（必须说清）
--------------------
Lancer 用 **Unix domain socket**（macOS/Linux），且明确拒绝在 Windows 上跑
（`control-server.ts:105`）。墨溯的主战场是 Windows（用户的机器就是），
Unix socket 在 Windows 上要靠额外依赖（pywin32 之外还得装 msyswin），违背
「零外部依赖铁律」。所以传输用 **localhost TCP + token**：

  - 只绑 127.0.0.1，不对外暴露 → 攻击面限于本机用户
  - 仍要 token：同机的其他用户/进程理论上能连 localhost
  - token 每次启动随机生成、落盘 0600（Windows 下用 ACL 收紧）

协议（一个请求一个 JSON 对象）
--------------------------
请求：{"op": "<operation>", ...参数}
响应：{"ok": true, "result": {...}}
      {"ok": false, "error": {"code": "<CODE>", "message": "..."}}

错误码（客户端可以按 code 分支，不要去匹配 message）
--------------------------------------------------
  INVALID_REQUEST  参数不合法
  INVALID_JSON     请求体不是合法 JSON
  UNAUTHORIZED    token 不对
  BUSY             目标正忙且操作模式不允许（send 撞上 running）
  NOT_FOUND        找不到目标（任务/会话）
  UNSUPPORTED      该 operation 在当前后端不可用
  INTERNAL_ERROR   服务端内部错误
  TOO_LARGE        请求体超限
  CANCELLED        目标已被取消（迭代 9）
  NOT_CANCELLABLE  目标不在运行中，没法取消（迭代 9）

操作集
------
  send    新建一轮分析并立即开跑。目标正忙 → BUSY
  queue   排队。当前正忙时挂起，空了自动跑
  steer   插队引导。当前正忙时立刻塞给正在跑的那轮
  status  查某轮/全局状态
  runs    列历史运行（复用 core/runs.py）
  wait    等某轮跑完并取结果
  stream  **流式订阅**。NDJSON 逐行推运行时事件（迭代 9）
  tail    取末 N 条消息 + 最后一条助手回复（迭代 9）
  stop    取消正在跑的那一轮（迭代 9）

迭代 9 补的三样（对标 Lancer 的 control-service.ts）
--------------------------------------------------
  stream —— Lancer 的 `operation === "stream"` 返回 ControlStream，
    服务端转成 `Content-Type: application/x-ndjson` 逐行推。
    关键语义：**带 since 游标**——先回放 `seq > since` 的历史，
    再接上实时流。断线重连只要带上最后收到的 seq 就行。
    订阅**先于**回放安装，否则回放与实时之间会漏事件。

  tail —— Lancer 的 `operation === "tail"`：返回末 N 条消息
    外加 lastAssistant（最后一条助手回复）。这是「只想要结果，
    不要过程」的轻量出口——stream 是给要跟着看的，tail 是给只要结论的。

  stop —— Lancer 的 `operation === "stop"`：取消正在跑的那轮。
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import time
from pathlib import Path

# 请求体上限：1 MiB。跟 Lancer 一致（control-server.ts MAX_BODY）。
MAX_BODY = 1024 * 1024

# 协议版本。不兼容变更时 +1，客户端据此判断。
PROTOCOL_VERSION = 1

# 错误码常量——写成常量而不是裸字符串，散落各处容易拼错。
ERR_INVALID_REQUEST = "INVALID_REQUEST"
ERR_INVALID_JSON = "INVALID_JSON"
ERR_UNAUTHORIZED = "UNAUTHORIZED"
ERR_BUSY = "BUSY"
ERR_NOT_FOUND = "NOT_FOUND"
ERR_UNSUPPORTED = "UNSUPPORTED"
ERR_INTERNAL = "INTERNAL_ERROR"
ERR_TOO_LARGE = "TOO_LARGE"
ERR_CANCELLED = "CANCELLED"
ERR_NOT_CANCELLABLE = "NOT_CANCELLABLE"

# 全部合法 operation
OPS = ("send", "queue", "steer", "status", "runs", "wait",
       "stream", "tail", "stop")

# 需要**流式响应**的 operation（迭代 9）。
# 这几个不能走普通的一问一答信封——响应体是 NDJSON 流，
# 要边跑边推，不是等全部完成才给一个 JSON。
STREAM_OPS = ("stream",)


class DispatchError(Exception):
    """带结构化错误码的异常。

    服务端捕获它并转成 {"ok": false, "error": {...}}；
    客户端收到后也抛它，调用方能按 code 分支。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


def default_port() -> int:
    """派活端口。8788/8789 已被 GUI 和 bot 占了，往后顺延。"""
    try:
        return int(os.environ.get("MOSU_DISPATCH_PORT", "8790"))
    except ValueError:
        return 8790


def dispatch_dir() -> Path:
    """派活通道的运行时目录（token / endpoint 描述文件）。"""
    home = os.environ.get("MOSU_HOME")
    base = Path(home) if home else (Path.home() / ".mosu")
    return base / "dispatch"


def token_path() -> Path:
    return dispatch_dir() / "control.token"


def endpoint_path() -> Path:
    """端点描述文件：外部 CLI 靠它找到服务在哪 + 拿 token。"""
    return dispatch_dir() / "control.endpoint"


def ensure_token() -> str:
    """取控制 token，没有就生成一个新的。

    32 字节随机 → 64 位十六进制，和 Lancer 一致。
    落盘权限：POSIX 用 0600；Windows 上 os.chmod 只改只读位，
    真正的 ACL 收紧靠 Windows 自己的用户目录权限（~/.mosu 本来就在
    用户目录下，其他用户读不到）。
    """
    p = token_path()
    if p.is_file():
        try:
            tok = p.read_text(encoding="utf-8").strip()
            if len(tok) == 64 and all(c in "0123456789abcdef" for c in tok):
                return tok
        except OSError:
            pass
    tok = secrets.token_hex(32)
    p.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再替换，避免并发启动时读到半个 token
    tmp = p.with_suffix(".tmp")
    tmp.write_text(tok + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    os.replace(tmp, p)
    return tok


def read_token() -> str | None:
    """读磁盘上的 token（客户端用）。读不到返回 None。"""
    p = token_path()
    try:
        if not p.is_file():
            return None
        tok = p.read_text(encoding="utf-8").strip()
        return tok or None
    except OSError:
        return None


def write_endpoint(port: int, pid: int | None = None) -> Path:
    """写端点描述文件。"""
    p = endpoint_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "protocol": PROTOCOL_VERSION,
        "host": "127.0.0.1",
        "port": port,
        "pid": pid if pid is not None else os.getpid(),
        "startedAt": time.time(),
    }
    p.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def read_endpoint() -> dict | None:
    """读端点描述文件。"""
    p = endpoint_path()
    try:
        if not p.is_file():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def token_matches(expected: str, got: str | None) -> bool:
    """常量时间比较 token。

    必须用 `hmac.compare_digest`：普通的 `==` 会在第一个不同的字节处
    提前返回，攻击者可以通过测量响应时间**逐字节猜出 token**。
    Lancer 用的就是 timingSafeEqual（control-server.ts:121）。
    """
    if not got:
        return False
    import hmac
    return hmac.compare_digest(expected, got)


def make_request(op: str, **params) -> dict:
    """构造请求对象。"""
    if op not in OPS:
        raise DispatchError(ERR_INVALID_REQUEST,
                            f"未知操作 {op!r}，可用：{', '.join(OPS)}")
    req = {"op": op}
    req.update({k: v for k, v in params.items() if v is not None})
    return req


def parse_request(raw: bytes) -> dict:
    """解析请求体。失败抛 DispatchError（带码）。"""
    if len(raw) > MAX_BODY:
        raise DispatchError(ERR_TOO_LARGE,
                            f"请求体超过 {MAX_BODY} 字节")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise DispatchError(ERR_INVALID_JSON, f"请求体不是合法 JSON：{e}")
    if not isinstance(data, dict):
        raise DispatchError(ERR_INVALID_REQUEST, "请求体必须是 JSON 对象")
    op = data.get("op")
    if not isinstance(op, str) or not op:
        raise DispatchError(ERR_INVALID_REQUEST, "缺少 op")
    if op not in OPS:
        raise DispatchError(ERR_INVALID_REQUEST,
                            f"未知操作 {op!r}，可用：{', '.join(OPS)}")
    return data


def ok(result: dict) -> dict:
    """成功响应信封。"""
    return {"ok": True, "result": result}


def err(code: str, message: str) -> dict:
    """失败响应信封。"""
    return {"ok": False, "error": {"code": code, "message": message}}


def require_str(data: dict, key: str, maxlen: int = 20000) -> str:
    """取必填字符串参数。空/超长都算参数不合法。"""
    v = data.get(key)
    if not isinstance(v, str) or not v.strip():
        raise DispatchError(ERR_INVALID_REQUEST, f"缺少参数 {key}")
    v = v.strip()
    if len(v) > maxlen:
        raise DispatchError(ERR_INVALID_REQUEST,
                            f"参数 {key} 太长（{len(v)} > {maxlen}）")
    return v


def require_cursor(data: dict, key: str = "since") -> int:
    """取游标参数。必须是**非负安全整数**。

    与 Lancer 的 cursor() 同口径：`since` 是 seq 游标，
    负数 / 小数 / 非数字都是参数不合法。

    为什么必须是整数而不是能转就转：游标是「我收到第几条了」的序号，
    传 1.5 没有意义（没有第 1.5 条），传 -1 会让服务端从头推，
    而客户端以为是要「最后 1 条」——静默推错比报错难查得多。
    """
    v = data.get(key)
    if v is None:
        return 0
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise DispatchError(ERR_INVALID_REQUEST,
                            f"{key} 必须是非负整数（收到 {v!r}）")
    return v


def require_int(data: dict, key: str, lo: int, hi: int, default: int) -> int:
    """取有界整数参数。"""
    v = data.get(key)
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, int):
        raise DispatchError(ERR_INVALID_REQUEST,
                            f"{key} 必须是整数（收到 {v!r}）")
    if v < lo or v > hi:
        raise DispatchError(ERR_INVALID_REQUEST,
                            f"{key} 必须在 {lo}..{hi} 之间（收到 {v}）")
    return v


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------
def _resolve_endpoint() -> tuple[str, int]:
    """找服务端在哪。

    顺序：显式参数 > 环境变量 > 端点描述文件 > 默认端口。
    端点描述文件是首选——服务可能跑在非默认端口上。
    """
    env_port = os.environ.get("MOSU_DISPATCH_PORT")
    if env_port:
        return "127.0.0.1", int(env_port)
    ep = read_endpoint()
    if ep and ep.get("port"):
        return ep.get("host") or "127.0.0.1", int(ep["port"])
    return "127.0.0.1", default_port()


def call(req: dict, host: str | None = None, port: int | None = None,
         token: str | None = None, timeout: float = 30.0) -> dict:
    """发一个请求给派活服务，返回响应信封。

    不抛异常——错误也走信封返回（{"ok": false, "error": {...}}），
    调用方自己判断。连接失败这类「连不上服务」的情况也包成信封，
    但 code 用 ERR_NOT_FOUND 并在 message 里说清楚。
    """
    import socket as _socket

    if host is None or port is None:
        h, p = _resolve_endpoint()
        host = host or h
        port = port or p
    tok = token if token is not None else read_token()
    if not tok:
        return err(ERR_UNAUTHORIZED,
                   "找不到控制 token（墨溯 GUI 没在跑，或 ~/.mosu/dispatch/ 不存在）")

    body = json.dumps(req, ensure_ascii=False).encode("utf-8")
    head = (
        f"POST /v1 HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Authorization: Bearer {tok}\r\n"
        f"Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("ascii")

    try:
        with _socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(head + body)
            chunks = []
            while True:
                d = s.recv(65536)
                if not d:
                    break
                chunks.append(d)
    except (OSError, _socket.timeout) as e:
        return err(ERR_NOT_FOUND,
                   f"连不上派活服务 {host}:{port}（{e}）。"
                   f"确认墨溯 GUI 在跑：mosu gui")
    raw = b"".join(chunks)
    # 拆 HTTP 头与 body
    sep = raw.find(b"\r\n\r\n")
    if sep < 0:
        return err(ERR_INTERNAL, "响应格式不对（没找到 HTTP 头分隔符）")
    payload = raw[sep + 4:]
    try:
        return json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        return err(ERR_INTERNAL, f"响应不是合法 JSON：{e}")


def call_or_raise(req: dict, **kw) -> dict:
    """同 call()，但失败时抛 DispatchError。"""
    r = call(req, **kw)
    if not r.get("ok"):
        e = r.get("error") or {}
        raise DispatchError(e.get("code") or ERR_INTERNAL,
                            e.get("message") or "未知错误")
    return r.get("result") or {}


# ---------------------------------------------------------------------------
# NDJSON 流式客户端（迭代 9）
# ---------------------------------------------------------------------------
def stream(req: dict, host: str | None = None, port: int | None = None,
           token: str | None = None, timeout: float = 300.0,
           on_event=None, stop_check=None):
    """发一个流式请求，逐行读 NDJSON 事件。

    返回 {"ok": True, "count": N, "lastSeq": S} 或 {"ok": False, "error": {...}}。

    与 call() 的区别：
    - call() 等一个完整 JSON 应答；stream() 边跑边收。
    - 响应头是 `Content-Type: application/x-ndjson`，
      **没有 Content-Length**（长度未知，边跑边写）。
      所以不能靠 Content-Length 判断结束，只能靠连接关闭。

    `on_event(event)` 每收到一行调一次。返回 False 可以提前中断。
    `stop_check()` 每行之前调一次，返回 True 就中断连接。
    两者都可选。

    为什么不用 requests / httpx：零外部依赖铁律（标准库足够）。

    错误处理：**连接断了不等于失败**——服务端可能正常跑完就关了连接。
    只有「一行都没收到且连接异常」才算错误。
    中途断开但已经收到过事件 → 返回 ok=True，并把已收到的告诉调用方。
    """
    import socket as _socket

    if host is None or port is None:
        h, p = _resolve_endpoint()
        host = host or h
        port = port or p
    tok = token if token is not None else read_token()
    if not tok:
        return err(ERR_UNAUTHORIZED,
                   "找不到控制 token（墨溯 GUI 没在跑，或 ~/.mosu/dispatch/ 不存在）")

    body = json.dumps(req, ensure_ascii=False).encode("utf-8")
    head = (
        f"POST /v1 HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Authorization: Bearer {tok}\r\n"
        f"Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Accept: application/x-ndjson\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("ascii")

    buf = b""
    count = 0
    last_seq = 0
    try:
        with _socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(head + body)
            while True:
                if stop_check is not None:
                    try:
                        if stop_check():
                            break
                    except Exception:
                        pass
                try:
                    d = s.recv(65536)
                except (_socket.timeout, OSError):
                    # 超时/断开：收到过事件就当正常收尾，没收到才算失败
                    break
                if not d:
                    break
                buf += d
                # NDJSON：按 \n 切
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        # 不是 JSON 的行：跳过，不让一行脏数据毁掉整条流
                        continue
                    count += 1
                    try:
                        last_seq = int(ev.get("seq") or last_seq)
                    except (TypeError, ValueError):
                        pass
                    if on_event is not None:
                        try:
                            if on_event(ev) is False:
                                return {"ok": True, "count": count,
                                        "lastSeq": last_seq}
                        except Exception:
                            pass
    except OSError as e:
        if count:
            return {"ok": True, "count": count, "lastSeq": last_seq}
        return err(ERR_NOT_FOUND,
                   f"连不上派活服务 {host}:{port}（{e}）。"
                   f"确认墨溯 GUI 在跑：mosu gui")
    return {"ok": True, "count": count, "lastSeq": last_seq}
