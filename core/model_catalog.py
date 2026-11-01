# -*- coding: utf-8 -*-
"""模型目录：模型家族识别 + 多端点目录组装 + 三态判定。

对标 Lancer 的 `apps/desktop/src/ui/model-families.ts`（32 行）与
`features/conversation/model-reel.tsx`（模型选择器 UI）。

## 为什么要有「模型目录」

墨溯原先只有一个 LLM 配置（baseUrl + apiKey + model），换模型要手改配置。
Lancer 的做法是：**端点（provider）→ 模型列表 → 家族分组**，用户在 UI 上选。
这带来三个墨溯原先没有的能力：

  1. **一个 Key 之外还能挂多个端点**（OpenAI 官方 / DeepSeek / 本地 vLLM…）
  2. **模型按家族归类**，跨端点也能看出「这是 Claude 系的」
  3. **三态可见**：可用 / 未启用 / 未配置——一眼知道哪个能点、哪个要先去配置

## 最关键的一条规则：模型名优先于 provider

Lancer 原注释：

    Transport providers (including OpenAI-compatible gateways) do not determine
    model identity when the actual model or its catalog label identifies a family.

翻译过来：**「走的哪个通道」不能决定「这是什么模型」**。
用户拿一个 OpenAI 兼容网关（`baseUrl` 是自建反代）转发 Claude，
provider 叫 `openai`，但模型是 `claude-sonnet-4`——家族必须判成 claude。

所以识别顺序是：**先看 modelId / label，匹配不到才看 provider**。
反过来就全错了——所有经兼容网关的模型会被归到「GPT」名下。

## 品牌标识为什么用文字徽标而不是 SVG

Lancer 给每个家族配了 SVG path（`ui/model-brand-marks.ts`）。墨溯零外部依赖、
前端无构建步骤，塞一批 SVG 会变成「资源文件 + 加载顺序」的新坑。所以这里用
**徽标文字 + 家族色**，信息量不减、实现成本为零。

纯标准库，不引入任何依赖。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

# --------------------------------------------------------------------------
# 9 个模型家族
#
# 两组正则的分工（Lancer 原样）：
#   provider_re —— 匹配 provider 名（端点标识），只在模型名匹配不上时才用
#   model_re    —— 匹配模型 id / 展示名
#
# 顺序有意义：Lancer 用两个平行数组靠下标对齐（MODEL_NAMES[i] 对应
# MODEL_FAMILIES[i]），改一个忘改另一个就会静默错位。这里把两套正则绑在
# 同一个 dict 里，从结构上杜绝错位。
# --------------------------------------------------------------------------
FAMILIES: tuple[dict[str, Any], ...] = (
    {"id": "openai", "label": "GPT", "badge": "GPT", "color": "#10a37f",
     "provider_re": re.compile(r"openai|gpt|\bo[134]", re.I),
     "model_re": re.compile(r"gpt|\bo[134](?:\b|[-_])", re.I)},
    {"id": "claude", "label": "Claude", "badge": "CL", "color": "#d97757",
     "provider_re": re.compile(r"anthropic|claude|opus|sonnet", re.I),
     "model_re": re.compile(r"claude|opus|sonnet|haiku", re.I)},
    {"id": "grok", "label": "Grok", "badge": "GX", "color": "#7c7c7c",
     "provider_re": re.compile(r"xai|grok", re.I),
     "model_re": re.compile(r"grok", re.I)},
    {"id": "gemini", "label": "Gemini", "badge": "GE", "color": "#4285f4",
     "provider_re": re.compile(r"google|gemini", re.I),
     "model_re": re.compile(r"gemini", re.I)},
    {"id": "doubao", "label": "豆包", "badge": "DB", "color": "#1664ff",
     "provider_re": re.compile(r"volcengine|doubao", re.I),
     "model_re": re.compile(r"doubao", re.I)},
    {"id": "kimi", "label": "Kimi", "badge": "KM", "color": "#1f1f1f",
     "provider_re": re.compile(r"moonshot|kimi", re.I),
     "model_re": re.compile(r"kimi", re.I)},
    {"id": "deepseek", "label": "DeepSeek", "badge": "DS", "color": "#4d6bfe",
     "provider_re": re.compile(r"deepseek", re.I),
     "model_re": re.compile(r"deepseek", re.I)},
    {"id": "qwen", "label": "Qwen", "badge": "QW", "color": "#615ced",
     "provider_re": re.compile(r"qwen|alibaba", re.I),
     "model_re": re.compile(r"qwen", re.I)},
    {"id": "zhipu", "label": "GLM", "badge": "GL", "color": "#3859ff",
     "provider_re": re.compile(r"zhipu|zai|z-ai|glm", re.I),
     "model_re": re.compile(r"glm", re.I)},
)

FAMILY_BY_ID = {f["id"]: f for f in FAMILIES}

# 三态：与 Lancer 的 model-reel 一一对应
STATE_AVAILABLE = "available"        # 可用（有 Key、在启用名单里）
STATE_DISABLED = "disabled"          # 未启用（端点通了，但模型被启用名单过滤掉）
STATE_UNCONFIGURED = "unconfigured"  # 未配置（端点没配 Key）

STATE_LABELS = {
    STATE_AVAILABLE: "可用",
    STATE_DISABLED: "未启用",
    STATE_UNCONFIGURED: "未配置",
}


# --------------------------------------------------------------------------
# 家族识别
# --------------------------------------------------------------------------
def model_family(provider: str = "", model_id: str = "",
                 label: str = "") -> dict[str, Any] | None:
    """判定模型所属家族；判不出来返回 None。

    顺序：先 modelId，再 label，最后才看 provider。
    理由见模块 docstring —— 兼容网关不决定模型身份。
    """
    for name in (model_id, label):
        if not name:
            continue
        for fam in FAMILIES:
            if fam["model_re"].search(name):
                return fam
    if provider:
        for fam in FAMILIES:
            if fam["provider_re"].search(provider):
                return fam
    return None


def family_id(provider: str = "", model_id: str = "",
              label: str = "") -> str:
    """只要家族 id（判不出来返回空串）。"""
    fam = model_family(provider, model_id, label)
    return fam["id"] if fam else ""


# --------------------------------------------------------------------------
# 启用名单匹配
#
# Lancer 用 enabledModelPatterns，元素形如 "providerId/modelId"。
# 墨溯沿用同一套语义，但放宽到支持两种写法：
#   "deepseek-chat"          —— 只看模型 id（跨端点通用，最常用）
#   "my-gateway/deepseek-chat" —— 精确到某个端点下的模型
# 另外支持 "*" 通配（"deepseek/*"）。
# --------------------------------------------------------------------------
def _pattern_matches(pattern: str, provider_id: str, model_id: str) -> bool:
    pat = (pattern or "").strip()
    if not pat:
        return False
    if pat == "*":
        return True
    if "/" in pat:
        p_pat, _, m_pat = pat.partition("/")
        if p_pat and p_pat != "*":
            if not re.fullmatch(_glob(p_pat), provider_id, re.I):
                return False
        return bool(re.fullmatch(_glob(m_pat), model_id, re.I))
    return bool(re.fullmatch(_glob(pat), model_id, re.I))


def _glob(pattern: str) -> str:
    """把 * ? 转成正则（其余字符转义）。"""
    out = []
    for ch in pattern:
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def is_model_enabled(patterns: list[str] | None, provider_id: str,
                     model_id: str) -> bool:
    """模型是否在启用名单内。名单为空 = 全部启用（Lancer 同语义）。"""
    pats = [p for p in (patterns or []) if isinstance(p, str) and p.strip()]
    if not pats:
        return True
    return any(_pattern_matches(p, provider_id, model_id) for p in pats)


# --------------------------------------------------------------------------
# 目录组装
# --------------------------------------------------------------------------
def endpoint_state(ep: dict[str, Any]) -> str:
    """端点自身的状态：没 Key = 未配置；被禁用 = 未启用；否则可用。"""
    if not str(ep.get("apiKey") or "").strip():
        return STATE_UNCONFIGURED
    if not ep.get("enabled", True):
        return STATE_DISABLED
    return STATE_AVAILABLE


def build_catalog(endpoints: list[dict[str, Any]] | None,
                  enabled_patterns: list[str] | None = None,
                  active_endpoint: str = "",
                  active_model: str = "") -> dict[str, Any]:
    """把端点列表组装成模型目录。

    返回结构（前端直接消费）：
      {
        "families": [{id,label,badge,color,count,models:[...]}],
        "endpoints": [{id,label,baseUrl,provider,state,stateLabel,hasKey,
                       modelCount,active}],
        "active": {endpointId, modelId} | None,
        "totalModels": int,
        "availableModels": int,
      }
    端点本身也是一种 provider，所以 provider_id 用端点 id。
    """
    eps = [e for e in (endpoints or []) if isinstance(e, dict)]
    patterns = enabled_patterns or []
    rows: list[dict[str, Any]] = []

    for ep in eps:
        ep_id = str(ep.get("id") or "").strip()
        if not ep_id:
            continue
        ep_state = endpoint_state(ep)
        ep_name = str(ep.get("label") or "").strip() or ep_id
        provider_hint = str(ep.get("provider") or "").strip()
        # 同一端点下重复的模型 id 要去重——用户手抖写两遍，
        # 列表里出现两条一模一样的项，看着像有两个不同的模型。
        seen: set[str] = set()
        for m in (ep.get("models") or []):
            if isinstance(m, dict):
                m_id = str(m.get("id") or "").strip()
                m_label = str(m.get("label") or "").strip()
            else:
                m_id = str(m or "").strip()
                m_label = ""
            if not m_id or m_id in seen:
                continue
            seen.add(m_id)
            fam = model_family(provider_hint, m_id, m_label)
            # 三态：端点没 Key → 未配置；端点可用但模型被名单过滤 → 未启用
            if ep_state == STATE_UNCONFIGURED:
                state = STATE_UNCONFIGURED
            elif ep_state == STATE_DISABLED:
                state = STATE_DISABLED
            elif is_model_enabled(patterns, ep_id, m_id):
                state = STATE_AVAILABLE
            else:
                state = STATE_DISABLED
            rows.append({
                "providerId": ep_id,
                "providerName": ep_name,
                "modelId": m_id,
                "label": m_label or m_id,
                "available": state == STATE_AVAILABLE,
                "state": state,
                "stateLabel": STATE_LABELS[state],
                "family": fam["id"] if fam else "",
                "familyLabel": fam["label"] if fam else "未归类",
                "familyBadge": fam["badge"] if fam else "?",
                "familyColor": fam["color"] if fam else "#8a8a8a",
            })

    # 按家族分组。归不了类的模型（family == ""）单独归到「未归类」组，
    # 不能因为判不出家族就丢弃 —— 用户配的模型他自己是知道的。
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["family"] or "", []).append(row)

    families_out: list[dict[str, Any]] = []
    for fam in FAMILIES:
        items = groups.get(fam["id"], [])
        families_out.append({
            "id": fam["id"], "label": fam["label"], "badge": fam["badge"],
            "color": fam["color"],
            "count": sum(1 for i in items if i["available"]),
            "modelCount": len(items),
            "models": items,
        })
    other = groups.get("", [])
    if other:
        families_out.append({
            "id": "", "label": "未归类", "badge": "?", "color": "#8a8a8a",
            "count": sum(1 for i in other if i["available"]),
            "modelCount": len(other),
            "models": other,
        })

    active = None
    if active_endpoint and active_model:
        active = {"endpointId": active_endpoint, "modelId": active_model}

    return {
        "families": families_out,
        "endpoints": [{
            "id": str(e.get("id") or ""),
            "label": str(e.get("label") or "").strip() or str(e.get("id") or ""),
            "baseUrl": str(e.get("baseUrl") or ""),
            "provider": str(e.get("provider") or ""),
            "state": endpoint_state(e),
            "stateLabel": STATE_LABELS[endpoint_state(e)],
            "hasKey": bool(str(e.get("apiKey") or "").strip()),
            "modelCount": len(e.get("models") or []),
            "active": bool(active and active["endpointId"] == str(e.get("id") or "")),
        } for e in eps if isinstance(e, dict) and str(e.get("id") or "").strip()],
        "active": active,
        "totalModels": len(rows),
        "availableModels": sum(1 for r in rows if r["available"]),
    }


# --------------------------------------------------------------------------
# 端点模型探测（GET {baseUrl}/models）
#
# OpenAI 兼容端点基本都提供这个接口，返回 {"data":[{"id":"..."}, ...]}。
# 探测是**可选增强**：失败不阻塞，用户也可以手工填模型名。
# --------------------------------------------------------------------------
def probe_models(base_url: str, api_key: str = "",
                 timeout: int = 20) -> dict[str, Any]:
    """拉取端点支持的模型列表。返回 {ok, models:[id], error, hint}。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return {"ok": False, "models": [], "error": "未填写接口地址",
                "hint": "先填 baseUrl（一般以 /v1 结尾）。"}
    url = base + "/models"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/120.0 Safari/537.36",
               "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "ignore")
        data = json.loads(raw)
    except urllib.error.HTTPError as e:
        hint = {
            401: "API Key 不对或已失效（401）。",
            403: "这个 Key 没有列模型的权限（403）。可以手工填模型名。",
            404: "该端点没有 /models 接口（404）。可以手工填模型名。",
        }.get(e.code, f"服务端返回 HTTP {e.code}。")
        return {"ok": False, "models": [], "error": f"HTTP {e.code}", "hint": hint}
    except urllib.error.URLError as e:
        return {"ok": False, "models": [], "error": f"{type(e).__name__}: {e.reason}",
                "hint": "连不上服务端。检查网络或 baseUrl 是否写错。"}
    except (TimeoutError, OSError) as e:
        return {"ok": False, "models": [], "error": f"{type(e).__name__}: {e}",
                "hint": "请求超时。可以手工填模型名。"}
    except json.JSONDecodeError:
        return {"ok": False, "models": [],
                "error": "返回的不是 JSON", "hint": "确认这是 OpenAI 兼容端点。"}
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "models": [], "error": f"{type(e).__name__}: {e}",
                "hint": "探测失败，可以手工填模型名。"}

    items = data.get("data") if isinstance(data, dict) else data
    ids: list[str] = []
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict):
                mid = str(it.get("id") or "").strip()
                if mid:
                    ids.append(mid)
            elif isinstance(it, str) and it.strip():
                ids.append(it.strip())
    if not ids:
        return {"ok": False, "models": [], "error": "响应里没有模型列表",
                "hint": "该端点可能不支持 /models。可以手工填模型名。"}
    return {"ok": True, "models": sorted(set(ids)), "error": "", "hint": ""}
