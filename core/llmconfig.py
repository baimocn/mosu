# -*- coding: utf-8 -*-
"""云端大模型配置（URL / API Key / 模型）的读写与连通性测试。

墨溯本身全离线能跑（解密、统计、画像都不需要大模型）。只有两类功能要联网调用：
  1. 数字分身对话（bot）
  2. 密钥获取失败时的 AI 辅助诊断

所以规则是：**需要时才要求配置，且必须由用户亲手填 URL / API Key**——
墨溯不内置任何服务端地址，也不替用户做决定。配置落在 ~/.mosu/config.json。

优先级：进程环境变量 > 配置文件 > 内置默认值（仅 base_url 有默认值）。

## 多端点（迭代 6）

原先只有一组 baseUrl / apiKey / model，想换模型得手动改配置。
现在支持**多个端点**（每个端点 = 一组地址+Key+模型列表），切换模型在 UI 上点一下。

配置形态（~/.mosu/config.json 的 llm 段）：
    {
      "endpoints": [
        {"id":"ep_ab12","label":"DeepSeek","baseUrl":"https://api.deepseek.com/v1",
         "apiKey":"sk-...","provider":"deepseek","models":["deepseek-chat"],
         "enabled":true}
      ],
      "activeEndpoint": "ep_ab12",
      "activeModel": "deepseek-chat",
      "enabledModels": ["deepseek-*"],       # 可选：模型启用名单，空 = 全启用
      "baseUrl": "...", "apiKey": "...", "model": "...",   # 旧扁平字段（同步写）
      "temperature": 0.8
    }

**向后兼容是硬要求**：
  - 旧的扁平配置（只有 baseUrl/apiKey/model）继续有效，
    读的时候自动「合成」成单个端点，不改写用户文件；
  - 切换活跃模型时**同时回写扁平字段**，这样任何仍读扁平字段的老代码
    （bot/agent 的 LlmClient）拿到的都是当前选中的模型；
  - 环境变量优先级不变，永远压过配置文件。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"

_ENV_KEYS = {
    "baseUrl": "MOSU_BOT_BASE_URL",
    "apiKey": "MOSU_BOT_API_KEY",
    "model": "MOSU_BOT_MODEL",
}


def _vault_path() -> Path:
    from core.dataset import KEYVAULT
    return KEYVAULT


# --------------------------------------------------------------------------
# 端点（多端点配置）
# --------------------------------------------------------------------------
def _normalize_endpoint(ep: dict) -> dict:
    """把一条端点记录规整成固定形状。脏数据不能让下游炸掉。"""
    ep = ep if isinstance(ep, dict) else {}
    models = []
    for m in (ep.get("models") or []):
        if isinstance(m, dict):
            mid = str(m.get("id") or "").strip()
        else:
            mid = str(m or "").strip()
        if mid and mid not in models:
            models.append(mid)
    return {
        "id": str(ep.get("id") or "").strip(),
        "label": str(ep.get("label") or "").strip(),
        "baseUrl": str(ep.get("baseUrl") or "").strip().rstrip("/"),
        "apiKey": str(ep.get("apiKey") or "").strip(),
        "provider": str(ep.get("provider") or "").strip(),
        "models": models,
        "enabled": ep.get("enabled", True) is not False,
    }


def _legacy_to_endpoint(file_cfg: dict) -> dict:
    """旧的扁平配置 → 一个合成端点。"""
    base = str(file_cfg.get("baseUrl") or "").strip().rstrip("/")
    model = str(file_cfg.get("model") or "").strip()
    return _normalize_endpoint({
        "id": "legacy",
        "label": "默认端点",
        "baseUrl": base,
        "apiKey": str(file_cfg.get("apiKey") or "").strip(),
        "provider": "",
        "models": [model] if model else [],
        "enabled": True,
    })


def load_endpoints() -> list[dict]:
    """读出端点列表。没有端点配置时，把旧的扁平配置合成一个返回。

    **不写回文件**——读操作不该改用户配置。
    """
    from core.dataset import load_keyvault
    file_cfg = load_keyvault().get("llm") or {}
    raw = file_cfg.get("endpoints")
    if isinstance(raw, list) and raw:
        out = []
        for ep in raw:
            norm = _normalize_endpoint(ep)
            if norm["id"]:
                out.append(norm)
        if out:
            return out
    legacy = _legacy_to_endpoint(file_cfg)
    return [legacy] if (legacy["baseUrl"] or legacy["apiKey"]) else []


def save_endpoint(endpoint: dict) -> list[dict]:
    """新增或更新一个端点（按 id 覆盖）。返回保存后的端点列表。"""
    from core.dataset import load_keyvault, save_keyvault
    norm = _normalize_endpoint(endpoint)
    if not norm["id"]:
        norm["id"] = "ep_" + uuid.uuid4().hex[:8]
    data = load_keyvault()
    cfg = dict(data.get("llm") or {})
    eps = [_normalize_endpoint(e) for e in (cfg.get("endpoints") or [])
           if isinstance(e, dict) and str(e.get("id") or "").strip()]
    replaced = False
    for i, e in enumerate(eps):
        if e["id"] == norm["id"]:
            # apiKey 传空串 = 不改动（前端回填的是掩码，不能把真值抹掉）
            if not norm["apiKey"]:
                norm["apiKey"] = e["apiKey"]
            eps[i] = norm
            replaced = True
            break
    if not replaced:
        eps.append(norm)
    cfg["endpoints"] = eps
    cfg["savedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data["llm"] = cfg
    save_keyvault(data)
    return eps


def delete_endpoint(endpoint_id: str) -> list[dict]:
    """删除一个端点。若删的是活跃端点，顺带把活跃指向清空。"""
    from core.dataset import load_keyvault, save_keyvault
    eid = (endpoint_id or "").strip()
    data = load_keyvault()
    cfg = dict(data.get("llm") or {})
    eps = [_normalize_endpoint(e) for e in (cfg.get("endpoints") or [])
           if isinstance(e, dict) and str(e.get("id") or "").strip()]
    eps = [e for e in eps if e["id"] != eid]
    cfg["endpoints"] = eps
    if cfg.get("activeEndpoint") == eid:
        cfg["activeEndpoint"] = ""
        cfg["activeModel"] = ""
    cfg["savedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data["llm"] = cfg
    save_keyvault(data)
    return eps


def set_active(endpoint_id: str, model_id: str) -> dict:
    """切换活跃端点与模型，并**同步回写扁平字段**。

    回写是向后兼容的关键：bot/agent 的 LlmClient 读的还是扁平字段，
    不同步的话 UI 显示选了 DeepSeek、实际跑的却是旧模型。
    """
    from core.dataset import load_keyvault, save_keyvault
    eid = (endpoint_id or "").strip()
    mid = (model_id or "").strip()
    data = load_keyvault()
    cfg = dict(data.get("llm") or {})
    eps = [_normalize_endpoint(e) for e in (cfg.get("endpoints") or [])
           if isinstance(e, dict) and str(e.get("id") or "").strip()]
    target = next((e for e in eps if e["id"] == eid), None)
    if target is None:
        raise ValueError("端点不存在：" + eid)
    if mid and mid not in target["models"]:
        target["models"].append(mid)
    cfg["endpoints"] = eps
    cfg["activeEndpoint"] = eid
    cfg["activeModel"] = mid
    # 同步扁平字段（老代码读这里）
    cfg["baseUrl"] = target["baseUrl"]
    cfg["apiKey"] = target["apiKey"]
    cfg["model"] = mid
    cfg["savedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data["llm"] = cfg
    save_keyvault(data)
    return {"endpointId": eid, "modelId": mid}


def set_enabled_models(patterns: list[str]) -> list[str]:
    """设置模型启用名单（空 = 全部启用）。"""
    from core.dataset import load_keyvault, save_keyvault
    data = load_keyvault()
    cfg = dict(data.get("llm") or {})
    pats = [str(p).strip() for p in (patterns or []) if str(p).strip()]
    cfg["enabledModels"] = pats
    cfg["savedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data["llm"] = cfg
    save_keyvault(data)
    return pats


def load_model_catalog() -> dict:
    """组装当前配置下的完整模型目录（家族分组 + 三态 + 端点列表）。"""
    from core.dataset import load_keyvault
    from core.model_catalog import build_catalog
    file_cfg = load_keyvault().get("llm") or {}
    eps = load_endpoints()
    active_ep = str(file_cfg.get("activeEndpoint") or "").strip()
    active_model = str(file_cfg.get("activeModel") or "").strip()
    if not active_ep and eps:
        # 没显式指定活跃端点时，取第一个有 Key 的端点的第一个模型
        for e in eps:
            if e["apiKey"] and e["models"]:
                active_ep, active_model = e["id"], e["models"][0]
                break
    patterns = file_cfg.get("enabledModels") or []
    return build_catalog(eps, patterns, active_ep, active_model)


# --------------------------------------------------------------------------
# 阶段模型（迭代 8）
# --------------------------------------------------------------------------
#
# Lancer 的 `LancerWorkflowStep.model?: {provider, modelId}`：
# 阶段模型是**步骤上可选的绑定**，不是流水线内置的。
# 切换靠 `setSessionModel(provider, modelId)`，
# 而 handoff 边的谓词 `distinct_configured_model` 要求
# **交付阶段必须用与上游不同的模型**——防单一模型偏见。
#
# 墨溯没有多步流水线（ReAct 是自主循环），但有一处正对的盲区：
# **取证和下结论是同一个模型**。它自己查的数据自己确认，
# 自己没法质疑自己。Lancer 那条谓词正是治这个的。
#
# 所以墨溯只开两个角色（不是四个阶段各配一个模型——
# 那样会变成固定管道，把自主性又绑回去）：
#   collect —— 跑 ReAct 循环、查工具、收证据
#   write   —— 基于已收集的证据写最终答案（**独立于采集者的自述**）
#
# 配置形态（~/.mosu/config.json 的 llm 段）：
#     "stageModels": {
#       "collect": {"model": "glm-5"},
#       "write":   {"model": "deepseek-chat"},
#       "write":   {"endpointId": "ep_x", "model": "deepseek-chat"}
#     }
#
# **不配置 = 现在的行为，一字不差**（向后兼容是硬要求）。
#
# 端点优先级铁律：`MOSU_BOT_BASE_URL` 环境变量仍然压过一切。
# 指定了 endpointId 但 env 也在 → **env 全赢**（baseUrl + apiKey 都用 env），
# 只保留模型名。绝不出现「一半 env 一半文件」的混合配置——
# 那种混合是最难排查的坑。

STAGE_MODEL_ROLES = ("collect", "write")

STAGE_ROLE_LABELS = {
    "collect": "取证模型",
    "write": "成文模型",
}


def load_stage_models() -> dict:
    """读阶段模型配置。返回 {"collect": {...}, "write": {...}}（可能缺键）。"""
    from core.dataset import load_keyvault
    file_cfg = load_keyvault().get("llm") or {}
    raw = file_cfg.get("stageModels")
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for role in STAGE_MODEL_ROLES:
        item = raw.get(role)
        if not isinstance(item, dict):
            continue
        mid = str(item.get("model") or "").strip()
        eid = str(item.get("endpointId") or "").strip()
        if not mid:
            continue
        out[role] = {"model": mid, "endpointId": eid}
    return out


def save_stage_models(models: dict) -> dict:
    """写入阶段模型配置。空值 = 取消该角色的绑定。

    **只写角色相关字段**，不动 endpoints / activeEndpoint 等其他键。
    """
    from core.dataset import load_keyvault, save_keyvault
    data = load_keyvault()
    cfg = dict(data.get("llm") or {})
    cur = dict(cfg.get("stageModels") or {})
    for role in STAGE_MODEL_ROLES:
        item = (models or {}).get(role)
        if not isinstance(item, dict):
            continue
        mid = str(item.get("model") or "").strip()
        eid = str(item.get("endpointId") or "").strip()
        if not mid:
            cur.pop(role, None)
            continue
        cur[role] = {"model": mid}
        if eid:
            cur[role]["endpointId"] = eid
    cfg["stageModels"] = cur
    cfg["savedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data["llm"] = cfg
    save_keyvault(data)
    return load_stage_models()


def resolve_stage_model(role: str, include_key: bool = False) -> dict | None:
    """解析某角色该用哪个模型。返回 None = 未配置（用活跃模型）。

    返回 {"role","model","endpointId","label","baseUrl","apiKey"}。
    include_key=False 时 apiKey 返回掩码（给前端展示用）。

    优先级铁律：**env > 显式 endpointId > 活跃端点**。
    env 存在时 baseUrl 与 apiKey **都**用 env，不做混合。
    """
    role = (role or "").strip()
    if role not in STAGE_MODEL_ROLES:
        return None
    spec = load_stage_models().get(role)
    if not spec:
        return None

    from core.dataset import load_keyvault
    file_cfg = load_keyvault().get("llm") or {}

    env_base = (os.environ.get("MOSU_BOT_BASE_URL") or "").strip()
    env_key = (os.environ.get("MOSU_BOT_API_KEY") or "").strip()

    if env_base or env_key:
        # env 全赢（baseUrl + key 一起），只保留模型名
        base = (env_base or DEFAULT_BASE_URL).rstrip("/")
        key = env_key
        eid = ""
        label = "环境变量"
    elif spec.get("endpointId"):
        eps = load_endpoints()
        target = next((e for e in eps if e["id"] == spec["endpointId"]), None)
        if target is None:
            # 端点不存在 → 不猜、不 fallback 到别的端点
            # （落到别的端点 = 用错 Key 调错服务，报错还难看）
            return None
        base = target["baseUrl"]
        key = target["apiKey"]
        eid = target["id"]
        label = target["label"] or target["id"]
    else:
        active = _active_flat(file_cfg)
        # 活跃端点没有 → 回落到旧扁平字段（向后兼容硬要求）
        base = active.get("baseUrl") or file_cfg.get("baseUrl") or DEFAULT_BASE_URL
        key = active.get("apiKey") or file_cfg.get("apiKey") or ""
        eid = str(file_cfg.get("activeEndpoint") or "").strip()
        label = "默认端点"

    return {
        "role": role,
        "model": spec["model"],
        "endpointId": eid,
        "label": label,
        "baseUrl": base.rstrip("/"),
        "apiKey": key if include_key else ("***" if key else ""),
    }


def stage_models_view() -> dict:
    """给前端看的阶段模型视图（含每个角色的解析结果与可用模型列表）。

    available 的来源（**必须包含该角色当前已保存的模型**）：
      1. 所有 enabled 端点里的模型（并集，不只第一个端点）
      2. 该角色已保存的模型本身

    为什么第 2 条必须有：端点模型表通常是探测出来的，可能没覆盖用户
    手工填的模型名；下拉里没有这一项 → `<option>` 缺失 →
    v-model 绑定的值在 select 里显示不出来（用户以为没保存）。
    """
    from core.dataset import load_keyvault
    eps = load_endpoints()
    file_cfg = load_keyvault().get("llm") or {}
    active_ep = str(file_cfg.get("activeEndpoint") or "").strip()
    saved = load_stage_models()

    # 所有 enabled 端点的模型并集
    pool: list[str] = []
    for e in eps:
        if not e.get("enabled", True):
            continue
        for m in (e.get("models") or []):
            if m and m not in pool:
                pool.append(m)

    out = {"roles": [], "configured": saved}
    for role in STAGE_MODEL_ROLES:
        resolved = resolve_stage_model(role)
        models: list[str] = []
        eid = (resolved or {}).get("endpointId") or ""
        if eid:
            target = next((e for e in eps if e["id"] == eid), None)
            models = [m for m in (target or {}).get("models") or [] if m]
        else:
            models = list(pool)
        # 端点没绑定时，也把活跃端点的模型补进来（扁平配置下 active_ep 为空，
        # 但 legacy 合成端点的 models 里就有当前模型）
        if not models and active_ep:
            target = next((e for e in eps if e["id"] == active_ep), None)
            models = [m for m in (target or {}).get("models") or [] if m]
        # 该角色已保存的模型必须在列表里（否则 select 显示不出来）
        cur = (resolved or {}).get("model") or ""
        if cur and cur not in models:
            models.insert(0, cur)
        out["roles"].append({
            "role": role,
            "label": STAGE_ROLE_LABELS.get(role, role),
            "model": cur,
            "endpointId": eid,
            "endpointLabel": (resolved or {}).get("label") or "",
            "baseUrl": (resolved or {}).get("baseUrl") or "",
            "available": models,
        })
    return out


def load_llm_config(include_key: bool = False) -> dict:
    """读取生效配置。include_key=False 时不返回 apiKey 明文（给前端展示用）。

    生效顺序：环境变量 > 活跃端点 > 旧扁平字段 > 内置默认值。

    **环境变量永远最高**——这是既有的铁律（`.env` 里的
    `MOSU_BOT_BASE_URL` 优先级高于 GUI 设置），多端点也不能破它。
    """
    from core.dataset import load_keyvault
    file_cfg = load_keyvault().get("llm") or {}
    # 活跃端点优先于扁平字段：UI 上切了模型，这里必须跟着变
    active = _active_flat(file_cfg)
    base = (os.environ.get(_ENV_KEYS["baseUrl"])
            or active.get("baseUrl")
            or file_cfg.get("baseUrl") or DEFAULT_BASE_URL).rstrip("/")
    key = (os.environ.get(_ENV_KEYS["apiKey"])
           or active.get("apiKey")
           or file_cfg.get("apiKey") or "")
    model = (os.environ.get(_ENV_KEYS["model"])
             or active.get("model")
             or file_cfg.get("model") or DEFAULT_MODEL)
    try:
        temp = float(os.environ.get("MOSU_BOT_TEMPERATURE")
                     or file_cfg.get("temperature") or 0.8)
    except (TypeError, ValueError):
        temp = 0.8
    env_set = any(os.environ.get(v) for v in _ENV_KEYS.values())
    return {
        "baseUrl": base,
        "apiKey": key if include_key else ("***" if key else ""),
        "model": model,
        "temperature": temp,
        "hasKey": bool(key),
        "source": ("env" if env_set
                   else ("active" if active else ("file" if file_cfg else "default"))),
        "savedAt": file_cfg.get("savedAt", ""),
        "activeEndpoint": file_cfg.get("activeEndpoint", "") or "",
        "activeModel": file_cfg.get("activeModel", "") or "",
    }


def _active_flat(file_cfg: dict) -> dict:
    """取活跃端点对应的扁平字段（baseUrl / apiKey / model）。

    找不到活跃端点时回落到旧扁平字段——旧配置必须继续能用。
    纯函数：只吃传进来的 file_cfg，不自己再去读文件（避免重复 IO 与快照不一致）。
    """
    active_ep = str(file_cfg.get("activeEndpoint") or "").strip()
    if not active_ep:
        return {}
    for ep in (file_cfg.get("endpoints") or []):
        if isinstance(ep, dict) and str(ep.get("id") or "").strip() == active_ep:
            norm = _normalize_endpoint(ep)
            return {"baseUrl": norm["baseUrl"], "apiKey": norm["apiKey"],
                    "model": str(file_cfg.get("activeModel") or "")}
    return {}


def save_llm_config(base_url: str = "", api_key: str = "", model: str = "",
                    temperature: float | None = None) -> dict:
    """写入配置文件（只写非空字段；api_key 为空串表示不改动）。"""
    from core.dataset import load_keyvault, save_keyvault
    data = load_keyvault()
    cfg = dict(data.get("llm") or {})
    if base_url:
        cfg["baseUrl"] = base_url.strip().rstrip("/")
    if api_key:
        cfg["apiKey"] = api_key.strip()
    if model:
        cfg["model"] = model.strip()
    if temperature is not None:
        cfg["temperature"] = float(temperature)
    cfg["savedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data["llm"] = cfg
    save_keyvault(data)
    return load_llm_config()


def clear_llm_config() -> None:
    from core.dataset import load_keyvault, save_keyvault
    data = load_keyvault()
    data.pop("llm", None)
    save_keyvault(data)


def test_connection(base_url: str = "", api_key: str = "", model: str = "",
                    timeout: int = 25) -> dict:
    """真实发一条最小请求验证配置可用（不会被当成"配了其实不通"）。

    返回 {ok, latencyMs, model, error, hint}。失败时给出人能行动的建议。
    """
    eff = load_llm_config(include_key=True)
    base = (base_url or eff["baseUrl"]).rstrip("/")
    key = api_key or eff["apiKey"]
    mdl = model or eff["model"]
    if not key:
        return {"ok": False, "error": "未填写 API Key",
                "hint": "在下方填入你的 API Key 后再测试。墨溯不会内置任何密钥。",
                "stage": "nokey"}
    url = f"{base}/chat/completions"
    body = json.dumps({
        "model": mdl,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 8, "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}",
                 "User-Agent": "Mozilla/5.0"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        txt = ""
        try:
            txt = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            pass
        return {"ok": True, "latencyMs": int((time.time() - t0) * 1000),
                "model": data.get("model", mdl), "reply": (txt or "")[:80],
                "baseUrl": base}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        hint = {
            401: "API Key 不对或已失效（401）。检查 Key 是否复制完整、是否有空格。",
            403: "没有该接口的权限（403）。确认账号余额/权限或换一个模型。",
            404: "地址或模型名不对（404）。baseUrl 一般以 /v1 结尾，模型名要服务端支持。",
            429: "触发限流（429）。稍后再试，或换一个额度充足的 Key。",
        }.get(e.code, f"服务端返回 HTTP {e.code}。")
        return {"ok": False, "error": f"HTTP {e.code}", "detail": detail,
                "hint": hint, "stage": "http", "baseUrl": base}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "hint": "连不上服务端。检查网络、代理，或 baseUrl 是否写错。",
                "stage": "network", "baseUrl": base}
    except Exception as e:                      # 解析失败等
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "hint": "服务端返回了无法解析的内容，确认这是 OpenAI 兼容端点。",
                "stage": "parse", "baseUrl": base}
