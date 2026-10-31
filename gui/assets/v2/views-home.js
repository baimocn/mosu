/* ==========================================================================
   墨溯 v2 · 首页
   设计意图：首屏回答「我有什么、我想干什么」，而不是「系统跑得怎么样」。
   ========================================================================== */
(function (global) {
  "use strict";
  const M = global.Mosu;
  const { ref, computed, onMounted } = Vue;

  const Home = {
    template: `
    <div class="v2-page">
      <div class="v2-home">
        <div class="v2-home-hero">
          <h1 class="v2-home-hello">{{ greeting }}</h1>
          <p class="v2-home-sub">
            {{ M.fmtNum(total) }} 条消息 · {{ M.fmtNum(sessions.length) }} 个会话 ·
            {{ M.fmtNum(contactCount) }} 位联系人，数据全部在本机。
          </p>
          <div class="v2-home-search">
            <span class="v2-home-search-icon" v-html="icon('search')"></span>
            <input class="v2-input" v-model="q" placeholder="搜索聊天记录、文件、语音…"
                   @keyup.enter="doSearch" />
          </div>
        </div>

        <div class="v2-home-grid">
          <!-- 左：最近会话，主战场 -->
          <div class="v2-card flush">
            <div class="v2-card-head" style="padding:16px 20px 12px;margin-bottom:0">
              <h2 class="v2-card-title">最近聊天</h2>
              <button class="v2-btn ghost sm" @click="goChat()">查看全部 →</button>
            </div>
            <div v-if="!sessions.length" class="v2-empty">
              <div class="v2-empty-title">还没有会话</div>
              <div class="v2-empty-desc">先到设置里配置数据库密钥</div>
            </div>
            <div v-else class="v2-home-recent">
              <button v-for="s in recent" :key="s.sessionId"
                      class="v2-list-item" @click="openSession(s)">
                <div class="v2-avatar sm" :style="{background: avatarColor(s.displayName)}">
                  {{ initial(s.displayName) }}
                </div>
                <div class="v2-list-item-main">
                  <div class="v2-list-item-top">
                    <span class="v2-list-item-name">{{ s.displayName }}</span>
                    <span class="v2-list-item-time">{{ fmtTime(s.lastTime) }}</span>
                  </div>
                  <div class="v2-list-item-sub">{{ s.lastMessage || '—' }}</div>
                </div>
                <span class="v2-list-item-count">{{ M.fmtNum(s.messageCount) }}</span>
              </button>
            </div>
          </div>

          <!-- 右：常用动作 + 数据速览 -->
          <div style="display:flex;flex-direction:column;gap:16px">
            <div class="v2-card">
              <h2 class="v2-card-title" style="margin-bottom:12px">快速开始</h2>
              <div class="v2-tile-grid">
                <button class="v2-home-tile" @click="goChat()">
                  <div class="v2-home-tile-icon" v-html="icon('chat')"></div>
                  <div class="v2-home-tile-text">
                    <div class="v2-home-tile-title">翻聊天记录</div>
                    <div class="v2-home-tile-desc">按会话阅读</div>
                  </div>
                </button>
                <button class="v2-home-tile" @click="goInsight()">
                  <div class="v2-home-tile-icon" v-html="icon('insight')"></div>
                  <div class="v2-home-tile-text">
                    <div class="v2-home-tile-title">看数据洞察</div>
                    <div class="v2-home-tile-desc">画像与年报</div>
                  </div>
                </button>
                <button class="v2-home-tile" @click="goShare()">
                  <div class="v2-home-tile-icon" v-html="icon('export')"></div>
                  <div class="v2-home-tile-text">
                    <div class="v2-home-tile-title">导出数据</div>
                    <div class="v2-home-tile-desc">HTML / CSV / JSON</div>
                  </div>
                </button>
                <button class="v2-home-tile" @click="goCorpus()">
                  <div class="v2-home-tile-icon" v-html="icon('corpus')"></div>
                  <div class="v2-home-tile-text">
                    <div class="v2-home-tile-title">生成语料包</div>
                    <div class="v2-home-tile-desc">喂给 AI 用</div>
                  </div>
                </button>
              </div>
            </div>

            <div class="v2-card">
              <h2 class="v2-card-title" style="margin-bottom:12px">数据速览</h2>
              <div class="v2-metrics" style="grid-template-columns:1fr 1fr;gap:12px">
                <div class="v2-metric accent">
                  <div class="v2-metric-val" style="font-size:18px">{{ M.fmtNum(total) }}</div>
                  <div class="v2-metric-label">消息总数</div>
                </div>
                <div class="v2-metric">
                  <div class="v2-metric-val" style="font-size:18px">{{ M.fmtNum(sessions.length) }}</div>
                  <div class="v2-metric-label">会话</div>
                </div>
                <div class="v2-metric">
                  <div class="v2-metric-val" style="font-size:18px">{{ M.fmtNum(contactCount) }}</div>
                  <div class="v2-metric-label">联系人</div>
                </div>
                <div class="v2-metric">
                  <div class="v2-metric-val" style="font-size:18px">{{ spanText }}</div>
                  <div class="v2-metric-label">记录跨度</div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>`,
    setup() {
      const q = ref("");
      const total = ref(0);
      const contactCount = ref(0);
      const firstTime = ref(0);
      const lastTime = ref(0);

      const sessions = computed(() => M.store.sessions);
      const recent = computed(() => sessions.value.slice(0, 12));

      const greeting = computed(() => {
        const h = new Date().getHours();
        if (h < 6) return "夜深了";
        if (h < 11) return "早上好";
        if (h < 14) return "中午好";
        if (h < 18) return "下午好";
        return "晚上好";
      });

      const spanText = computed(() => {
        if (!firstTime.value || !lastTime.value) return "—";
        const days = Math.round((lastTime.value - firstTime.value) / 86400);
        if (days < 60) return days + " 天";
        return Math.round(days / 30) + " 个月";
      });

      function goChat() { location.hash = "#/chat"; }
      function goInsight() { location.hash = "#/insight"; }
      function goShare() { location.hash = "#/share"; }
      function goCorpus() { location.hash = "#/share.corpus"; }

      function openSession(s) {
        location.hash = "#/chat";
        // 让聊天页等一拍再选中，避免组件尚未挂载
        setTimeout(() => {
          global.MosuV2.selectSession(s.sessionId);
        }, 60);
      }

      async function doSearch() {
        const kw = q.value.trim();
        if (!kw) return;
        location.hash = "#/chat";
        setTimeout(() => { global.MosuV2.setSearch(kw); }, 60);
      }

      onMounted(async () => {
        await M.refreshSessions();
        // 统计总消息数与联系人：优先用 /api/status，失败则降级为会话数估算
        const st = M.store.status;
        if (st && st.data) {
          if (st.data.messageCount) total.value = st.data.messageCount;
          if (st.data.contactCount) contactCount.value = st.data.contactCount;
          if (st.data.firstTime) firstTime.value = st.data.firstTime;
          if (st.data.lastTime) lastTime.value = st.data.lastTime;
        }
        if (!total.value) {
          total.value = sessions.value.reduce((a, s) => a + (s.messageCount || 0), 0);
        }
        if (!contactCount.value) {
          try {
            const c = await M.api.get("/api/contacts", { limit: 1 });
            contactCount.value = c.total || 0;
          } catch (e) { /* 忽略 */ }
        }
        if (!firstTime.value || !lastTime.value) {
          try {
            const p = await M.api.get("/api/profile");
            if (p && p.overview) {
              firstTime.value = p.overview.firstText ? Date.parse(p.overview.firstText.replace(/-/g, "/")) / 1000 : 0;
              lastTime.value = p.overview.lastText ? Date.parse(p.overview.lastText.replace(/-/g, "/")) / 1000 : 0;
              if (!total.value && p.overview.totalMessages) total.value = p.overview.totalMessages;
            }
          } catch (e) { /* 忽略 */ }
        }
      });

      return {
        q, total, contactCount, spanText, sessions, recent, greeting,
        goChat, goInsight, goShare, goCorpus, openSession, doSearch,
        M, icon: (n) => global.MosuIcons[n] || "",
        avatarColor: M.avatarColor, initial: M.initial,
        fmtTime: M.fmtTime,
      };
    },
  };

  global.MosuV2 = global.MosuV2 || {};
  global.MosuV2.Home = Home;
})(window);
