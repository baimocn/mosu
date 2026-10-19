/* ==========================================================================
   墨溯 · 前端内核
   只放三样东西：API 封装、全局状态、通用工具。页面组件一律从这里取，
   不允许各写各的 fetch——网络错误与提示必须有一致的行为。
   ========================================================================== */
(function (global) {
  "use strict";
  const { reactive, ref } = Vue;

  /* ---------------------------------------------------------------- 网络 */
  async function request(url, opts) {
    const r = await fetch(url, opts);
    const ct = r.headers.get("content-type") || "";
    if (!r.ok) {
      let msg = `HTTP ${r.status}`;
      try {
        const d = ct.includes("json") ? await r.json() : null;
        if (d && d.error) msg = d.error;
      } catch (e) { /* 错误体不是 JSON，用状态码兜底 */ }
      throw new Error(msg);
    }
    if (r.status === 204) return null;
    return ct.includes("json") ? r.json() : r.text();
  }

  const api = {
    get(path, params) {
      const q = params
        ? "?" + Object.entries(params)
            .filter(([, v]) => v !== undefined && v !== null && v !== "")
            .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
            .join("&")
        : "";
      return request(path + q);
    },
    post(path, body) {
      return request(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
    },
    /** SSE：逐事件回调，返回可关闭的句柄。用于密钥扫描这类长任务。 */
    stream(path, handlers) {
      const es = new EventSource(path);
      for (const [evt, fn] of Object.entries(handlers)) {
        es.addEventListener(evt, (e) => {
          try { fn(JSON.parse(e.data)); } catch (_) { fn({}); }
        });
      }
      es.onerror = () => { es.close(); };
      return es;
    },
  };

  /* ---------------------------------------------------------------- 提示 */
  const toasts = reactive([]);
  let seq = 0;
  function toast(message, kind = "info", timeout = 3200) {
    const id = ++seq;
    toasts.push({ id, message, kind });
    setTimeout(() => {
      const i = toasts.findIndex((t) => t.id === id);
      if (i >= 0) toasts.splice(i, 1);
    }, timeout);
    return id;
  }
  toast.ok = (m) => toast(m, "success");
  toast.err = (m) => toast(m, "error", 5200);

  /* ---------------------------------------------------------------- 主题 */
  const THEME_KEY = "mosu.theme";
  const theme = ref(localStorage.getItem(THEME_KEY) || "auto");

  function effectiveTheme() {
    if (theme.value === "auto") {
      return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    return theme.value;
  }
  function applyTheme() {
    document.documentElement.setAttribute("data-theme", effectiveTheme());
  }
  function setTheme(t) {
    theme.value = t;
    localStorage.setItem(THEME_KEY, t);
    applyTheme();
  }
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (theme.value === "auto") applyTheme();
  });

  /* ---------------------------------------------------------------- 状态 */
  const store = reactive({
    ready: false,          // 后端数据层是否可用
    status: null,          // /api/status
    config: null,          // /api/config
    accounts: null,        // /api/accounts
    sessions: [],
    sessionsLoaded: false,
    route: "",             // 当前页
    busy: 0,               // 全局进行中计数（>0 时顶栏显示）
    llm: null,             // 大模型配置
  });

  async function refreshStatus() {
    try {
      store.status = await api.get("/api/status");
      store.ready = !!(store.status && store.status.data && store.status.data.ready);
    } catch (e) {
      store.ready = false;
    }
    return store.status;
  }
  async function refreshConfig() {
    try { store.config = await api.get("/api/config"); } catch (e) { /* 忽略 */ }
    try { store.accounts = await api.get("/api/accounts"); } catch (e) { /* 忽略 */ }
    try { store.llm = await api.get("/api/llm"); } catch (e) { /* 忽略 */ }
  }
  async function refreshSessions(force) {
    if (store.sessionsLoaded && !force) return store.sessions;
    try {
      const d = await api.get("/api/sessions", { limit: 500 });
      store.sessions = d.sessions || [];
      store.sessionsLoaded = true;
    } catch (e) { /* 忽略 */ }
    return store.sessions;
  }

  /* ---------------------------------------------------------------- 工具 */
  function pad(n) { return n < 10 ? "0" + n : String(n); }
  function fmtTime(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    if (sameDay) return hm;
    const y = new Date(now.getTime() - 86400000);
    if (d.toDateString() === y.toDateString()) return `昨天 ${hm}`;
    if (d.getFullYear() === now.getFullYear()) {
      return `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
    }
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`;
  }
  function fmtDay(ts) {
    const d = new Date(ts * 1000);
    const now = new Date();
    if (d.toDateString() === now.toDateString()) return "今天";
    const y = new Date(now.getTime() - 86400000);
    if (d.toDateString() === y.toDateString()) return "昨天";
    if (d.getFullYear() === now.getFullYear()) return `${d.getMonth() + 1}月${d.getDate()}日`;
    return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
  }
  function fmtNum(n) {
    if (n === null || n === undefined) return "—";
    const x = Number(n);
    if (Math.abs(x) >= 1e8) return (x / 1e8).toFixed(2) + " 亿";
    if (Math.abs(x) >= 1e4) return (x / 1e4).toFixed(1) + " 万";
    return x.toLocaleString("zh-CN");
  }
  function fmtSize(n) {
    if (!n) return "—";
    const u = ["B", "KB", "MB", "GB"];
    let i = 0, x = Number(n);
    while (x >= 1024 && i < u.length - 1) { x /= 1024; i++; }
    return `${x.toFixed(i ? 1 : 0)} ${u[i]}`;
  }
  function fmtDur(ms) {
    if (!ms || ms < 1000) return `${Math.round(ms || 0)}ms`;
    const s = ms / 1000;
    if (s < 60) return s.toFixed(1) + "s";
    return `${Math.floor(s / 60)}分${Math.round(s % 60)}秒`;
  }
  function debounce(fn, wait = 240) {
    let t = null;
    return function (...a) {
      clearTimeout(t);
      t = setTimeout(() => fn.apply(this, a), wait);
    };
  }
  /** 搜索高亮：先转义再插入 <mark>，避免 XSS */
  function highlight(text, kw) {
    if (!text) return "";
    const esc = String(text).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    if (!kw) return esc;
    const k = kw.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return esc.replace(new RegExp(k, "gi"), (m) => `<mark>${m}</mark>`);
  }
  function initial(name) {
    const s = String(name || "?").trim();
    return s ? s[0].toUpperCase() : "?";
  }
  /** 头像色：由名字哈希出稳定色，避免每次渲染跳色 */
  function avatarColor(name) {
    let h = 0;
    for (const ch of String(name || "")) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    const hues = [210, 145, 275, 20, 340, 190, 45];
    return `hsl(${hues[h % hues.length]} 62% 92%)`;
  }

  global.Mosu = {
    api, toast, toasts, store, theme, setTheme, effectiveTheme, applyTheme,
    refreshStatus, refreshConfig, refreshSessions,
    fmtTime, fmtDay, fmtNum, fmtSize, fmtDur, debounce, highlight, initial, avatarColor,
  };
})(window);
