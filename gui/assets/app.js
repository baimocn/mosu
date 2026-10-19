/* ==========================================================================
   墨溯 · 应用外壳：导航 / 路由 / 命令面板 / 首启判定
   ========================================================================== */
(function (global) {
  "use strict";
  const M = global.Mosu;
  const V = global.MosuViews;
  const { createApp, computed, ref, watch, onMounted } = Vue;

  const NAV = [
    { group: "开始", items: [
      { id: "console", name: "控制台", icon: "◎", comp: "Console" },
      { id: "browse", name: "数据浏览", icon: "💬", comp: "Browse" },
      { id: "search", name: "全库搜索", icon: "🔍", comp: "Browse", params: { search: 1 } },
    ] },
    { group: "洞察", items: [
      { id: "stats", name: "数据统计", icon: "📊", comp: "Stats" },
      { id: "profile", name: "画像", icon: "🧭", comp: "Insight", props: { kind: "profile" } },
      { id: "report", name: "年报", icon: "📅", comp: "Insight", props: { kind: "report" } },
      { id: "contacts", name: "联系人", icon: "👤", comp: "Contacts" },
    ] },
    { group: "数据", items: [
      { id: "moments", name: "朋友圈", icon: "🌐", comp: "Moments" },
      { id: "export", name: "导出", icon: "📦", comp: "ExportView" },
    ] },
    { group: "系统", items: [
      { id: "settings", name: "设置", icon: "⚙", comp: "Settings" },
    ] },
  ];

  const App = {
    components: {
      Console: V.Console, Browse: V.Browse, Stats: V.Stats, Insight: V.Insight,
      Contacts: V.Contacts, Moments: V.Moments, ExportView: V.ExportView,
      Settings: V.Settings, Wizard: V.Wizard,
    },
    template: `
    <div class="app" :class="{collapsed: collapsed}">
      <!-- 首启向导：数据未就绪时占满全屏 -->
      <wizard v-if="showWizard" @done="finishWizard"
              style="position:fixed;inset:0;z-index:500;background:var(--bg-app)"></wizard>

      <aside class="sidebar">
        <div class="brand">
          <div class="brand-mark">墨</div>
          <div class="brand-text">
            <div class="brand-name">墨溯</div>
            <div class="brand-sub">本地微信数据</div>
          </div>
        </div>
        <nav class="nav">
          <div v-for="g in nav" :key="g.group" class="nav-group">
            <div class="nav-group-title">{{ g.group }}</div>
            <button v-for="it in g.items" :key="it.id"
                    class="nav-item" :class="{active: route===it.id}"
                    @click="go(it.id)">
              <span class="nav-icon">{{ it.icon }}</span>
              <span class="nav-label">{{ it.name }}</span>
              <span v-if="it.id==='browse' && sessionCount" class="nav-badge">
                {{ sessionCount }}
              </span>
            </button>
          </div>
        </nav>
        <div class="side-foot">
          <button class="nav-item" @click="collapsed=!collapsed">
            <span class="nav-icon">{{ collapsed ? '»' : '«' }}</span>
            <span class="nav-label side-foot-text">收起</span>
          </button>
          <button class="nav-item" @click="toggleTheme">
            <span class="nav-icon">{{ theme==='dark' ? '☀' : '☾' }}</span>
            <span class="nav-label side-foot-text">
              {{ theme==='dark' ? '浅色' : '深色' }}
            </span>
          </button>
        </div>
      </aside>

      <div class="main">
        <header class="topbar">
          <button class="btn btn-ghost btn-icon" @click="cmdkOpen=true" title="命令面板 Ctrl+K">⌘</button>
          <div>
            <div class="topbar-title">{{ currentName }}</div>
            <div class="topbar-sub">{{ statusLine }}</div>
          </div>
          <div class="topbar-spacer"></div>
          <span class="badge" :class="ready ? 'badge-success' : 'badge-warn'">
            {{ ready ? '数据就绪' : '未配置' }}
          </span>
          <button class="btn btn-sm" @click="go('settings')">设置</button>
        </header>

        <main class="content">
          <component :is="currentComp" v-bind="currentProps" :key="route" />
        </main>
      </div>

      <!-- 命令面板 -->
      <div v-if="cmdkOpen" class="overlay" @click.self="cmdkOpen=false">
        <div class="cmdk" @click.stop>
          <input class="cmdk-input" v-model="cmdkQ" ref="cmdkInput"
                 placeholder="跳转到…（输入关键词，回车进入）" @keyup.esc="cmdkOpen=false" />
          <div class="cmdk-list">
            <div v-for="(r,i) in cmdkResults" :key="r.id"
                 class="cmdk-item" :class="{active: i===cmdkIdx}"
                 @click="pickCmd(r)" @mouseenter="cmdkIdx=i">
              <span class="nav-icon">{{ r.icon }}</span>
              <span>{{ r.name }}</span>
              <span class="cmdk-key">{{ r.group }}</span>
            </div>
            <div v-if="!cmdkResults.length" class="tiny muted" style="padding:12px">
              没有匹配的页面
            </div>
          </div>
        </div>
      </div>

      <!-- Toast -->
      <div class="toasts">
        <div v-for="t in toasts" :key="t.id" class="toast" :class="'toast-'+t.kind">
          {{ t.message }}
        </div>
      </div>
    </div>`,
    setup() {
      const route = ref("console");
      const collapsed = ref(localStorage.getItem("mosu.collapsed") === "1");
      const cmdkOpen = ref(false);
      const cmdkQ = ref("");
      const cmdkIdx = ref(0);
      const showWizard = ref(false);
      const booting = ref(true);

      const flat = NAV.flatMap((g) => g.items.map((i) => ({ ...i, group: g.group })));
      const current = computed(() => flat.find((i) => i.id === route.value) || flat[0]);
      const currentName = computed(() => (current.value ? current.value.name : ""));
      const currentComp = computed(() => (current.value ? current.value.comp : "Console"));
      const currentProps = computed(() => (current.value ? (current.value.props || {}) : {}));

      function go(id) {
        route.value = id;
        location.hash = "#/" + id;
      }
      function parseHash() {
        const m = (location.hash || "").match(/^#\/([a-z]+)/);
        if (m && flat.some((i) => i.id === m[1])) route.value = m[1];
      }

      const sessionCount = computed(() => M.store.sessions.length);
      const ready = computed(() => M.store.ready);
      const statusLine = computed(() => {
        const s = M.store.status;
        if (!s) return "正在连接本地服务…";
        const d = s.data || {};
        if (!d.ready) return d.error || "数据未就绪，去设置里配置密钥";
        const w = s.wechat || {};
        return `${M.fmtNum(sessionCount.value)} 个会话 · 微信通道${w.logged_in ? "已连接" : "未连接"}`;
      });

      const cmdkResults = computed(() => {
        const q = cmdkQ.value.trim().toLowerCase();
        if (!q) return flat;
        return flat.filter((i) => i.name.toLowerCase().includes(q) || i.id.includes(q));
      });
      function pickCmd(r) { go(r.id); cmdkOpen.value = false; cmdkQ.value = ""; }

      function toggleTheme() {
        const order = ["light", "dark", "auto"];
        const i = order.indexOf(M.theme.value);
        M.setTheme(order[(i + 1) % order.length]);
      }

      async function finishWizard() {
        showWizard.value = false;
        await M.refreshConfig();
        await M.refreshStatus();
        await M.refreshSessions(true);
        go("browse");
      }

      onMounted(async () => {
        parseHash();
        window.addEventListener("hashchange", parseHash);
        document.addEventListener("keydown", (e) => {
          if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
            e.preventDefault();
            cmdkOpen.value = !cmdkOpen.value;
            if (cmdkOpen.value) {
              Vue.nextTick(() => {
                const el = document.querySelector(".cmdk-input");
                if (el) el.focus();
              });
            }
          }
          if (e.key === "Escape") { cmdkOpen.value = false; }
        });
        watch(collapsed, (v) => localStorage.setItem("mosu.collapsed", v ? "1" : "0"));

        await M.refreshStatus();
        await M.refreshConfig();
        const st = M.store.status;
        const ok = st && st.data && st.data.ready;
        booting.value = false;
        if (!ok) {
          showWizard.value = true;
        } else {
          await M.refreshSessions();
        }
      });

      return {
        nav: NAV, route, go, collapsed, currentName, currentComp, currentProps,
        statusLine, ready, sessionCount, toasts: M.toasts, theme: M.theme,
        cmdkOpen, cmdkQ, cmdkIdx, cmdkResults, pickCmd, toggleTheme,
        showWizard, finishWizard, booting,
      };
    },
  };

  M.applyTheme();
  createApp(App).mount("#app");
})(window);
