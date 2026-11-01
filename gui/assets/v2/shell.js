/* ==========================================================================
   墨溯 v2 · 应用外壳
   侧栏从 13 个平铺入口归并为 5 个任务入口；系统状态收进顶栏折叠区，
   首屏让位给数据本身。
   ========================================================================== */
(function (global) {
  "use strict";
  const M = global.Mosu;
  const V = global.MosuV2;
  const { createApp, computed, ref, watch, onMounted, nextTick } = Vue;

  /* 五个入口 = 五件用户想干的事 */
  const NAV = [
    { id: "home", num: "1", label: "首页", icon: "home", comp: "Home" },
    { id: "chat", num: "2", label: "聊天", icon: "chat", comp: "Chat" },
    { id: "insight", num: "3", label: "洞察", icon: "insight", comp: "Insight" },
    { id: "share", num: "4", label: "分享", icon: "export", comp: "Share" },
    { id: "settings", num: "5", label: "设置", icon: "settings", comp: "Settings" },
  ];

  /* 每个入口下的二级视图（用于命令面板与路由寻址） */
  const SUB = [
    { id: "home", group: "首页", name: "首页", icon: "home" },
    { id: "chat", group: "聊天", name: "会话与搜索", icon: "chat" },
    { id: "chat.contacts", group: "聊天", name: "联系人", icon: "contacts" },
    { id: "insight", group: "洞察", name: "提问分析", icon: "insight" },
    { id: "insight.raw", group: "洞察", name: "原始统计图表", icon: "stats" },
    { id: "share", group: "分享", name: "导出数据", icon: "export" },
    { id: "share.corpus", group: "分享", name: "个人语料包", icon: "corpus" },
    { id: "share.moments", group: "分享", name: "朋友圈", icon: "moments" },
    { id: "settings", group: "设置", name: "账号与密钥", icon: "settings" },
    { id: "settings.channel", group: "设置", name: "微信通道", icon: "wechat" },
    { id: "settings.mcp", group: "设置", name: "MCP 服务", icon: "plug" },
  ];

  const App = {
    components: {
      Home: V.Home, Chat: V.Chat, Insight: V.Insight, Share: V.Share, Settings: V.Settings,
      Lightbox: global.MosuViews.Lightbox,
      // 分支树组件（洞察页用）：泳道 + 语义锚点光线
      "branch-tree": global.MosuV2.BranchTree,
    },
    template: `
    <div class="v2-app">
      <aside class="v2-side" :class="{collapsed: collapsed}">
        <div class="v2-brand">
          <img src="/assets/logo-512.png" alt="墨溯" />
          <div class="v2-brand-text">
            <div class="v2-brand-name">墨溯</div>
            <div class="v2-brand-sub">本地微信数据</div>
          </div>
        </div>
        <nav class="v2-nav">
          <button v-for="it in nav" :key="it.id"
                  class="v2-nav-item" :class="{active: isActive(it.id)}"
                  @click="go(it.id)">
            <span class="v2-nav-num">{{ it.num }}</span>
            <span class="v2-nav-label">{{ it.label }}</span>
          </button>
        </nav>
        <div class="v2-side-foot">
          <button class="v2-nav-item" @click="cmdkOpen=true">
            <span class="v2-nav-num">/</span>
            <span class="v2-nav-label">命令面板</span>
          </button>
          <button class="v2-nav-item" @click="toggleTheme">
            <span class="v2-nav-num" v-html="icon(theme==='dark' ? 'sun' : 'moon')"></span>
            <span class="v2-nav-label">{{ theme==='dark' ? '浅色' : '深色' }}</span>
          </button>
        </div>
      </aside>

      <div class="v2-main">
        <header class="v2-topbar">
          <button class="v2-btn ghost sm" @click="cmdkOpen=true" title="命令面板 Ctrl+K">
            <span v-html="icon('command')"></span>
          </button>
          <div>
            <div class="v2-topbar-title">{{ currentTitle }}</div>
            <div class="v2-topbar-sub">{{ statusLine }}</div>
          </div>
          <div class="v2-topbar-spacer"></div>

          <!-- 系统状态：折叠在右上角，不再占首屏 -->
          <div style="position:relative">
            <button class="v2-status-pill" @click="statusOpen=!statusOpen">
              <span class="v2-dot" :class="ready ? 'ok' : 'err'"></span>
              <span>{{ ready ? '数据就绪' : '未配置' }}</span>
              <span v-html="icon('chevronDown')"></span>
            </button>
            <div v-if="statusOpen" class="v2-status-pop" @click.stop>
              <div class="v2-card-title" style="margin-bottom:8px">系统状态</div>
              <div v-for="(r,i) in statusRows" :key="i" class="v2-status-row">
                <span class="k">{{ r.k }}</span>
                <span class="v">{{ r.v }}</span>
              </div>
              <div style="display:flex;gap:8px;margin-top:14px">
                <button class="v2-btn sm" @click="statusOpen=false; go('settings.channel')">微信通道</button>
                <button class="v2-btn sm" @click="statusOpen=false; go('settings.mcp')">MCP 服务</button>
              </div>
            </div>
          </div>
        </header>

        <main class="v2-content" @click="statusOpen=false">
          <component :is="currentComp" v-bind="currentProps" :key="route" />
        </main>
      </div>

      <!-- 命令面板 -->
      <div v-if="cmdkOpen" class="v2-cmdk-overlay" @click.self="cmdkOpen=false">
        <div class="v2-cmdk" @click.stop>
          <input class="v2-cmdk-input" v-model="cmdkQ" ref="cmdkInput"
                 placeholder="跳转到…（输入关键词，回车进入）" @keyup.esc="cmdkOpen=false" />
          <div class="v2-cmdk-list">
            <div v-for="(r,i) in cmdkResults" :key="r.id"
                 class="v2-cmdk-item" :class="{active: i===cmdkIdx}"
                 @click="pickCmd(r)" @mouseenter="cmdkIdx=i">
              <span class="cmdk-num">{{ r.group }}</span>
              <span>{{ r.name }}</span>
            </div>
            <div v-if="!cmdkResults.length" class="tiny muted" style="padding:12px">没有匹配的页面</div>
          </div>
        </div>
      </div>

      <div class="v2-toasts">
        <div v-for="t in toasts" :key="t.id" class="v2-toast" :class="'v2-toast-'+t.kind">
          {{ t.message }}
        </div>
      </div>
    </div>`,
    setup() {
      const route = ref("home");
      const collapsed = ref(localStorage.getItem("mosu.v2.collapsed") === "1");
      const cmdkOpen = ref(false);
      const cmdkQ = ref("");
      const cmdkIdx = ref(0);
      const statusOpen = ref(false);

      const current = computed(() => {
        const base = route.value.split(".")[0];
        return NAV.find((n) => n.id === base) || NAV[0];
      });
      const currentComp = computed(() => V[current.value.comp]);
      const currentProps = computed(() => ({ route: route.value }));
      const currentTitle = computed(() => {
        const sub = SUB.find((s) => s.id === route.value);
        return sub ? sub.name : current.value.label;
      });

      const isActive = (id) => route.value === id || route.value.startsWith(id + ".");
      const ready = computed(() => M.store.ready);
      const sessionCount = computed(() => M.store.sessions.length);

      const statusLine = computed(() => {
        const s = M.store.status;
        if (!s) return "正在连接本地服务…";
        const d = s.data || {};
        if (!d.ready) return d.error || "数据未就绪，去设置里配置密钥";
        const w = s.wechat || {};
        return `${M.fmtNum(sessionCount.value)} 个会话 · 微信通道${w.logged_in ? "已连接" : "未连接"}`;
      });

      const statusRows = computed(() => {
        const s = M.store.status;
        if (!s) return [{ k: "状态", v: "连接中…" }];
        const d = s.data || {}, w = s.wechat || {}, sv = s.service || {};
        const rows = [
          { k: "数据源", v: d.ready ? "已就绪" : (d.error || "未就绪") },
          { k: "会话数", v: M.fmtNum(sessionCount.value) },
          { k: "消息样本", v: d.messageCount ? M.fmtNum(d.messageCount) : "—" },
          { k: "微信通道", v: w.logged_in ? ("已连接 · " + (w.account || "")) : "未登录" },
          { k: "通道服务", v: sv.alive ? "运行中" : "未启动" },
        ];
        if (w.user) rows.push({ k: "通道账号", v: w.user });
        return rows;
      });

      function go(id) {
        route.value = id;
        location.hash = "#/" + id;
        statusOpen.value = false;
      }
      function parseHash() {
        const m = (location.hash || "").match(/^#\/([a-z.]+)/);
        if (m && SUB.some((s) => s.id === m[1])) route.value = m[1];
      }

      const cmdkResults = computed(() => {
        const q = cmdkQ.value.trim().toLowerCase();
        if (!q) return SUB;
        return SUB.filter((i) => i.name.toLowerCase().includes(q) || i.group.includes(q) || i.id.includes(q));
      });
      function pickCmd(r) { go(r.id); cmdkOpen.value = false; cmdkQ.value = ""; cmdkIdx.value = 0; }

      function toggleTheme() {
        const order = ["light", "dark", "auto"];
        const i = order.indexOf(M.theme.value);
        M.setTheme(order[(i + 1) % order.length]);
      }

      onMounted(async () => {
        parseHash();
        window.addEventListener("hashchange", parseHash);
        document.addEventListener("keydown", (e) => {
          if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
            e.preventDefault();
            cmdkOpen.value = !cmdkOpen.value;
            if (cmdkOpen.value) {
              nextTick(() => { const el = document.querySelector(".v2-cmdk-input"); if (el) el.focus(); });
            }
          }
          if (e.key === "Escape") { cmdkOpen.value = false; statusOpen.value = false; }
        });
        watch(collapsed, (v) => localStorage.setItem("mosu.v2.collapsed", v ? "1" : "0"));

        await M.refreshStatus();
        await M.refreshConfig();
        if (M.store.ready) await M.refreshSessions();
      });

      return {
        nav: NAV, route, go, collapsed, currentTitle, currentComp, currentProps, isActive,
        statusLine, statusRows, ready, statusOpen, toasts: M.toasts, theme: M.theme,
        cmdkOpen, cmdkQ, cmdkIdx, cmdkResults, pickCmd, toggleTheme,
        icon: (n) => global.MosuIcons[n] || "",
      };
    },
  };

  M.applyTheme();
  const app = createApp(App);
  app.config.errorHandler = function (err, instance, info) {
    var msg = (err && (err.message || String(err))) || "未知错误";
    try { if (global.Mosu && global.Mosu.toast) global.Mosu.toast.err("界面出错：" + msg); } catch (_) {}
    var host = document.getElementById("app");
    if (!host || host.querySelector(".v2-fatal")) return;
    var box = document.createElement("div");
    box.className = "v2-fatal";
    box.innerHTML =
      '<div class="v2-fatal-card">'
      + '<div class="v2-fatal-mark">!</div>'
      + '<div class="v2-fatal-title">这个页面出错了</div>'
      + '<div class="v2-fatal-msg"></div>'
      + '<button class="v2-btn primary" id="v2-fatal-reload">重新加载</button>'
      + '</div>';
    box.querySelector(".v2-fatal-msg").textContent = msg + (info ? "（" + info + "）" : "");
    box.querySelector("#v2-fatal-reload").onclick = function () { location.reload(); };
    host.appendChild(box);
    if (window.console && console.error) console.error("[mosu-v2] render error:", err, info);
  };
  app.mount("#app");
})(window);
