/* ==========================================================================
   墨溯 v2 · 聊天工作台
   合并了原「数据浏览」「全库搜索」「联系人」三页：
     左栏 = 会话列表（内含搜索框与类型筛选，搜索不再另开一页）
     中栏 = 消息流
     右栏 = 会话详情 + 快捷操作（消灭原来右侧 60% 的空置）
   ========================================================================== */
(function (global) {
  "use strict";
  const M = global.Mosu;
  const { ref, computed, watch, onMounted, nextTick, getCurrentInstance } = Vue;

  const Chat = {
    components: { Lightbox: global.MosuViews.Lightbox },
    template: `
    <div class="v2-ws">
      <!-- 左：会话 + 搜索 -->
      <div class="v2-ws-left">
        <div class="v2-ws-search">
          <input class="v2-input" v-model="kw" placeholder="搜索全部聊天记录…"
                 @keyup.enter="runSearch" />
        </div>
        <div class="v2-ws-filter">
          <div class="v2-seg" style="width:100%">
            <button :class="{active: filter==='all'}" @click="filter='all'">全部</button>
            <button :class="{active: filter==='group'}" @click="filter='group'">群聊</button>
            <button :class="{active: filter==='single'}" @click="filter='single'">私聊</button>
            <button :class="{active: filter==='mine'}" @click="filter='mine'">我的</button>
          </div>
        </div>

        <div class="v2-ws-list v2-scroll">
          <!-- 搜索模式 -->
          <template v-if="searching">
            <div style="padding:10px 14px;font-size:12px;color:var(--text-4)">
              找到 {{ searchTotal }} 条结果
              <button class="v2-btn ghost sm" style="float:right;padding:2px 8px"
                      @click="clearSearch">清除</button>
            </div>
            <div v-if="!searchResults.length" class="v2-empty" style="padding:32px 16px">
              <div class="v2-empty-desc">没有匹配的聊天内容</div>
            </div>
            <button v-for="r in searchResults" :key="r.key" class="v2-list-item" @click="openHit(r)">
              <div class="v2-avatar xs" :style="{background: avatarColor(r.sessionName)}">
                {{ initial(r.sessionName) }}
              </div>
              <div class="v2-list-item-main">
                <div class="v2-list-item-top">
                  <span class="v2-list-item-name">{{ r.sessionName }}</span>
                  <span class="v2-list-item-time">{{ fmtTime(r.createTime) }}</span>
                </div>
                <div class="v2-list-item-sub" v-html="highlight(r.snippet, kw)"></div>
              </div>
            </button>
          </template>

          <!-- 会话模式 -->
          <template v-else>
            <button v-for="s in filtered" :key="s.sessionId"
                    class="v2-list-item" :class="{active: cur && cur.sessionId===s.sessionId}"
                    @click="pick(s)">
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
            <div v-if="!filtered.length" class="v2-empty" style="padding:32px 16px">
              <div class="v2-empty-desc">没有符合条件的会话</div>
            </div>
          </template>
        </div>
      </div>

      <!-- 中：消息流 -->
      <div class="v2-ws-mid v2-scroll" ref="mid">
        <div v-if="!cur" class="v2-empty" style="margin:auto">
          <div v-html="icon('chat')" style="opacity:.35"></div>
          <div class="v2-empty-title">选一个会话开始阅读</div>
          <div class="v2-empty-desc">左侧是全部会话，上方搜索框可以搜全库聊天内容</div>
        </div>

        <template v-else>
          <div style="padding:14px 20px;border-bottom:1px solid var(--border);
                      display:flex;align-items:center;gap:12px;flex-shrink:0;background:var(--bg-surface)">
            <div class="v2-avatar sm" :style="{background: avatarColor(cur.displayName)}">
              {{ initial(cur.displayName) }}
            </div>
            <div style="flex:1;min-width:0">
              <div style="font-size:14px;font-weight:600;color:var(--text-1)">{{ cur.displayName }}</div>
              <div style="font-size:12px;color:var(--text-4)">
                {{ cur.type==='group' ? '群聊' : '私聊' }} · {{ M.fmtNum(cur.messageCount) }} 条消息
              </div>
            </div>
            <div class="v2-seg">
              <button :class="{active: order==='asc'}" @click="setOrder('asc')">正序</button>
              <button :class="{active: order==='desc'}" @click="setOrder('desc')">倒序</button>
            </div>
            <button class="v2-btn sm" @click="exportCur" title="导出该会话">导出</button>
          </div>

          <div style="flex:1;padding:16px 20px">
            <div v-if="loading" style="display:flex;flex-direction:column;gap:12px">
              <div v-for="i in 6" :key="i" class="v2-skel" :style="{height:'44px',width:(60+ (i%3)*15)+'%'}"></div>
            </div>
            <div v-else-if="!messages.length" class="v2-empty">
              <div class="v2-empty-title">没有消息</div>
            </div>
            <template v-else>
              <div v-for="m in messages" :key="m.id" class="v2-msg" :class="{mine: m.mine}">
                <div class="v2-avatar xs" :style="{background: avatarColor(m.mine ? '我' : (m.senderName||cur.displayName))}">
                  {{ initial(m.mine ? '我' : (m.senderName||cur.displayName)) }}
                </div>
                <div class="v2-msg-body">
                  <div v-if="!m.mine && cur.type==='group'" class="v2-msg-name">{{ m.senderName || '未知' }}</div>
                  <!-- 文本 -->
                  <div v-if="m.kind==='text'" class="v2-bubble" v-html="highlight(m.text, kw)"></div>
                  <!-- 图片 -->
                  <div v-else-if="m.kind==='image'" class="v2-media" style="width:180px;height:180px">
                    <img v-if="m.mediaOk" :src="'/api/media?md5='+m.md5" :alt="'图片'"
                         @click="openMedia(m)" style="cursor:pointer" />
                    <span v-else>图片不可用</span>
                  </div>
                  <!-- 视频 -->
                  <div v-else-if="m.kind==='video'" class="v2-media" style="width:200px;height:112px">视频暂不支持预览</div>
                  <!-- 语音 -->
                  <div v-else-if="m.kind==='voice'" class="v2-bubble" style="display:flex;align-items:center;gap:8px">
                    <span v-html="icon('voice')"></span>
                    <span>语音 {{ m.seconds ? m.seconds+'″' : '' }}</span>
                  </div>
                  <!-- 文件 -->
                  <div v-else-if="m.kind==='file'" class="v2-bubble">📎 {{ m.fileName || '文件' }}</div>
                  <!-- 其他 -->
                  <div v-else class="v2-bubble" style="color:var(--text-4)">[{{ m.kindText || m.kind }}]</div>
                  <div class="v2-msg-time">{{ fmtTime(m.createTime) }}</div>
                </div>
              </div>
              <div v-if="hasMore" style="text-align:center;padding:16px 0">
                <button class="v2-btn sm" @click="loadMore" :disabled="loading">
                  {{ loading ? '加载中…' : '加载更多' }}
                </button>
              </div>
            </template>
          </div>
        </template>
      </div>

      <!-- 右：会话详情 / 引导 -->
      <div class="v2-ws-right v2-scroll">
        <div class="v2-detail" v-if="cur">
          <div class="v2-detail-head">
            <div class="v2-avatar lg" :style="{background: avatarColor(cur.displayName)}">
              {{ initial(cur.displayName) }}
            </div>
            <div style="min-width:0">
              <div class="v2-detail-name">{{ cur.displayName }}</div>
              <div class="v2-detail-sub">{{ cur.type==='group' ? '群聊' : '私聊' }}</div>
            </div>
          </div>

          <div class="v2-kv"><span class="k">消息总数</span><span class="v">{{ M.fmtNum(cur.messageCount) }}</span></div>
          <div class="v2-kv"><span class="k">会话 ID</span><span class="v">{{ cur.sessionId }}</span></div>
          <div class="v2-kv" v-if="cur.shards && cur.shards.length">
            <span class="k">存储分片</span><span class="v">{{ cur.shards.length }} 个</span>
          </div>

          <div style="margin-top:20px;display:flex;flex-direction:column;gap:8px">
            <button class="v2-btn" @click="exportCur">导出这个会话</button>
            <button class="v2-btn" @click="openContact">查看联系人资料</button>
            <button class="v2-btn ghost" @click="searchInCur">只搜这个会话</button>
          </div>

          <div style="margin-top:24px;padding-top:16px;border-top:1px solid var(--border)">
            <div class="v2-card-title" style="margin-bottom:10px">消息构成</div>
            <div class="v2-bar-row" v-for="(c,i) in kindBreakdown" :key="i">
              <span class="v2-bar-label">{{ c.name }}</span>
              <span class="v2-bar-track"><span class="v2-bar-fill" :style="{width: c.pct+'%'}"></span></span>
              <span class="v2-bar-val">{{ c.n }}</span>
            </div>
          </div>
        </div>

        <!-- 未选会话：右栏给引导，不留空白 -->
        <div class="v2-detail" v-else>
          <div class="v2-card-title" style="margin-bottom:12px">怎么用</div>
          <div class="v2-insight-list">
            <div class="v2-insight-item">
              <span class="v2-insight-mark"></span>
              <span>在左侧点一个会话，右侧会显示消息原文，图片能直接解密预览。</span>
            </div>
            <div class="v2-insight-item">
              <span class="v2-insight-mark"></span>
              <span>上方搜索框搜的是<b>全库</b>所有聊天内容，不只是当前会话。</span>
            </div>
            <div class="v2-insight-item">
              <span class="v2-insight-mark"></span>
              <span>「我的」筛选只看自己发出的消息，适合盘点自己的表达习惯。</span>
            </div>
            <div class="v2-insight-item">
              <span class="v2-insight-mark"></span>
              <span>选中会话后可一键导出 HTML / CSV / JSON。</span>
            </div>
          </div>

          <div style="margin-top:24px;padding-top:16px;border-top:1px solid var(--border)">
            <div class="v2-card-title" style="margin-bottom:10px">库里有</div>
            <div class="v2-kv"><span class="k">会话</span><span class="v">{{ M.fmtNum(sessions.length) }}</span></div>
            <div class="v2-kv"><span class="k">消息总量</span><span class="v">{{ M.fmtNum(totalMessages) }}</span></div>
          </div>
        </div>
      </div>

      <lightbox v-if="lb.show" :src="lb.src" :meta="lb.meta" @close="lb.show=false" />
    </div>`,
    setup(props) {
      const proxy = getCurrentInstance().proxy;
      const kw = ref("");
      const filter = ref("all");
      const cur = ref(null);
      const messages = ref([]);
      const offset = ref(0);
      const hasMore = ref(false);
      const loading = ref(false);
      const order = ref("desc");
      const searching = ref(false);
      const searchResults = ref([]);
      const searchTotal = ref(0);
      const lb = ref({ show: false, src: "", meta: null });

      const sessions = computed(() => M.store.sessions);

      // 消息总量：优先用 status，没有则按会话求和（列表接口本来就带 messageCount）
      const totalMessages = computed(() => {
        const st = M.store.status;
        if (st && st.data && st.data.messageCount) return st.data.messageCount;
        return sessions.value.reduce((a, s) => a + (s.messageCount || 0), 0);
      });

      const filtered = computed(() => {
        let list = sessions.value;
        if (filter.value === "group") list = list.filter((s) => s.type === "group");
        else if (filter.value === "single") list = list.filter((s) => s.type !== "group");
        return list;
      });

      const kindBreakdown = computed(() => {
        const map = {};
        messages.value.forEach((m) => { map[m.kind] = (map[m.kind] || 0) + 1; });
        const total = messages.value.length || 1;
        const names = {
          text: "文本", image: "图片", voice: "语音", video: "视频",
          file: "文件", other: "其他", system: "系统",
        };
        return Object.entries(map)
          .map(([k, n]) => ({ name: names[k] || k, n, pct: Math.round(n / total * 100) }))
          .sort((a, b) => b.n - a.n);
      });

      async function pick(s) {
        cur.value = s;
        offset.value = 0;
        messages.value = [];
        await loadMessages();
      }

      async function loadMessages() {
        if (!cur.value) return;
        loading.value = true;
        try {
          const d = await M.api.get("/api/messages", {
            session: cur.value.sessionId,
            limit: 50,
            offset: offset.value,
          });
          const list = d.messages || [];
          messages.value = offset.value === 0 ? list : messages.value.concat(list);
          hasMore.value = !!d.hasMore;
          offset.value += list.length;
          // 倒序时保持视口贴底
          await nextTick();
          const mid = this.$refs.mid;
          if (mid) mid.scrollTop = mid.scrollHeight;
        } catch (e) {
          M.toast.err("加载消息失败：" + e.message);
        } finally {
          loading.value = false;
        }
      }

      async function loadMore() {
        await loadMessages();
      }

      function setOrder(o) {
        order.value = o;
        messages.value = [];
        offset.value = 0;
        loadMessages();
      }

      async function runSearch() {
        const q = kw.value.trim();
        if (!q) { clearSearch(); return; }
        searching.value = true;
        try {
          const d = await M.api.get("/api/search", { q, limit: 100 });
          searchResults.value = (d.messages || []).map((m, i) => ({
            key: (m.sessionId || "") + "_" + (m.createTime || i),
            sessionId: m.sessionId,
            sessionName: m.sessionName || m.sessionId || "未知会话",
            createTime: m.createTime,
            snippet: m.content || m.text || "",
          }));
          searchTotal.value = d.total || 0;
        } catch (e) {
          M.toast.err("搜索失败：" + e.message);
        }
      }

      function clearSearch() {
        kw.value = "";
        searching.value = false;
        searchResults.value = [];
        searchTotal.value = 0;
      }

      function openHit(r) {
        const s = sessions.value.find((x) => x.sessionId === r.sessionId);
        if (s) { searching.value = false; pick(s); }
      }

      function searchInCur() {
        if (!cur.value) return;
        kw.value = cur.value.displayName;
        runSearch();
      }

      function openContact() {
        location.hash = "#/chat.contacts";
        setTimeout(() => { global.MosuV2.selectSession(cur.value.sessionId); }, 60);
      }

      function exportCur() {
        location.hash = "#/share";
        setTimeout(() => { global.MosuV2.setExportSession(cur.value.sessionId); }, 60);
      }

      function openMedia(m) {
        lb.value = { show: true, src: "/api/media?md5=" + m.md5, meta: { name: "图片", time: m.createTime } };
      }

      onMounted(async () => {
        // 把 setup 暴露出去，供 bridge.js 跨页调用（选会话 / 搜索）
        global.MosuV2.Chat.__instance = proxy;

        await M.refreshSessions();
        // 支持从首页带会话 ID 进来
        const pending = global.MosuV2.pendingSession;
        if (pending) {
          global.MosuV2.pendingSession = null;
          const s = sessions.value.find((x) => x.sessionId === pending);
          if (s) pick(s);
        }
        const pendingKw = global.MosuV2.pendingSearch;
        if (pendingKw) {
          global.MosuV2.pendingSearch = null;
          kw.value = pendingKw;
          runSearch();
        }
      });

      watch(() => props.route, () => {
        // 从联系人页跳回时保持会话
      });

      return {
        kw, filter, cur, messages, hasMore, loading, order, searching,
        searchResults, searchTotal, lb, sessions, filtered, kindBreakdown, totalMessages,
        pick, loadMessages, loadMore, setOrder, runSearch, clearSearch,
        openHit, searchInCur, openContact, exportCur, openMedia,
        M, icon: (n) => global.MosuIcons[n] || "",
        avatarColor: M.avatarColor, initial: M.initial,
        fmtTime: M.fmtTime, fmtDay: M.fmtDay, highlight: M.highlight,
      };
    },
  };

  global.MosuV2 = global.MosuV2 || {};
  global.MosuV2.Chat = Chat;
})(window);
