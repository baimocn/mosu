/* ==========================================================================
   页面 · 数据浏览（会话列表 + 聊天流 + 全库搜索）
   交互要点：
   - 会话列表支持搜索与高亮，切换会话不丢筛选条件
   - 聊天流按天分隔、同一人连续消息归并（5 分钟内），向上翻页保持滚动位置
   - 图片点击进灯箱；加载失败明确占位，不留空白方块
   ========================================================================== */
(function (global) {
  "use strict";
  const M = global.Mosu;
  const T = (s) => s;   // 预留：若将来做多语言，这里替换

  /* 空态插画：细线风，随主题变色（currentColor） */
  const ART = {
    search: `<svg viewBox="0 0 132 96" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round">
      <circle cx="58" cy="42" r="24"/><path d="M76 60l18 18"/>
      <path d="M48 42h20M58 32v20" opacity=".45"/>
      <circle cx="104" cy="24" r="2" opacity=".4"/><circle cx="26" cy="70" r="2" opacity=".4"/>
    </svg>`,
    chat: `<svg viewBox="0 0 132 96" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
      <rect x="18" y="22" width="58" height="38" rx="10" opacity=".55"/>
      <path d="M32 40h30M32 49h20" opacity=".4"/>
      <rect x="60" y="42" width="54" height="34" rx="10"/>
      <path d="M72 58h30M72 66h18" opacity=".55"/>
    </svg>`,
    folder: `<svg viewBox="0 0 132 96" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
      <path d="M22 32l8-10h26l6 8h44a6 6 0 016 6v34a6 6 0 01-6 6H28a6 6 0 01-6-6V32z"/>
      <path d="M46 58h40M66 48v20" opacity=".45"/>
    </svg>`,
  };

  const Browse = {
    components: { Lightbox: global.MosuViews.Lightbox },
    // searchOnMount：从左侧导航「全库搜索」进来时置 1，挂载后自动弹出搜索框。
    // 之前导航项写的是 params:{search:1}，而 app.js 的 currentProps 只读 props，
    // params 是永远传不进来的死字段 —— 于是 #/search 只是把同一个组件再渲染一遍，
    // 点了什么都不会发生。改成 props 才能真正把意图传进来。
    // 注意名字不能叫 openSearch：那个是 methods 里的方法名，同名会被覆盖。
    props: { searchOnMount: { type: [Number, Boolean], default: 0 } },
    template: `
    <div class="browse">
      <!-- 左：会话列表 -->
      <div class="session-pane">
        <div class="pane-head">
          <input class="input" v-model="kw" placeholder="搜索会话…" />
          <div class="row" style="margin-top:8px">
            <button class="chip" :class="{active: filter==='all'}" @click="filter='all'">全部</button>
            <button class="chip" :class="{active: filter==='private'}" @click="filter='private'">单聊</button>
            <button class="chip" :class="{active: filter==='group'}" @click="filter='group'">群聊</button>
            <span class="spacer"></span>
            <span class="tiny muted">{{ shown.length }} 个</span>
          </div>
        </div>
        <div class="pane-list">
          <div v-if="loading" style="padding:16px">
            <div v-for="i in 8" class="skeleton" :key="i"
                 style="height:52px;margin-bottom:8px"></div>
          </div>
          <div v-else-if="!shown.length" class="empty">
            <div class="empty-art" v-html="art.search"></div>
            <div class="empty-title">没有匹配的会话</div>
            <div class="empty-desc">换个关键词，或确认数据目录与密钥是否正确。</div>
          </div>
          <div v-for="s in shown" :key="s.sessionId"
               class="session-item" :class="{active: s.sessionId===curId}"
               @click="pick(s)">
            <div class="avatar" :style="{background: avatarBg(s)}">
              <img v-if="s.avatarUrl" :src="s.avatarUrl" @error="s.avatarUrl=''" />
              <span v-else>{{ initial(s.displayName) }}</span>
            </div>
            <div class="session-meta">
              <div class="session-name" v-html="hl(s.displayName)"></div>
              <div class="session-preview" v-html="hl(s.summary || '')"></div>
            </div>
            <div class="session-time">{{ fmtTime(s.lastTime) }}</div>
          </div>
        </div>
      </div>

      <!-- 右：聊天流 -->
      <div class="chat-pane">
        <template v-if="cur">
          <div class="topbar" style="border-bottom:1px solid var(--border)">
            <div>
              <div class="topbar-title">{{ cur.displayName }}</div>
              <div class="topbar-sub">
                {{ typeLabel(cur) }} · {{ fmtNum(cur.messageCount) }} 条消息
                <span v-if="msgs.length">· 已载入 {{ msgs.length }} 条</span>
              </div>
            </div>
            <div class="spacer"></div>
            <select class="select" style="width:110px" v-model="fType" @change="reload">
              <option value="">全部类型</option>
              <option value="text">文本</option>
              <option value="image">图片</option>
              <option value="voice">语音</option>
              <option value="video">视频</option>
              <option value="file">文件</option>
              <option value="emoji">表情</option>
            </select>
            <select class="select" style="width:110px" v-model="fDir" @change="reload">
              <option value="">全部方向</option>
              <option value="in">对方</option>
              <option value="out">我</option>
            </select>
            <input class="input" style="width:150px" v-model="fFrom" placeholder="开始日期"
                   onfocus="this.type='date'" @change="reload" />
            <input class="input" style="width:150px" v-model="fTo" placeholder="结束日期"
                   onfocus="this.type='date'" @change="reload" />
            <button class="btn btn-sm" @click="openSearch">全库搜索</button>
          </div>

          <div class="chat-scroll" ref="scroller" @scroll="onScroll">
            <div v-if="hasMore" style="text-align:center;padding:8px">
              <button class="btn btn-sm" @click="loadMore" :disabled="loadingMore">
                <span v-if="loadingMore" class="spinner"></span>
                {{ loadingMore ? '载入中' : '载入更早的消息' }}
              </button>
            </div>
            <div v-else style="text-align:center;padding:8px" class="tiny muted">已到最早</div>

            <div v-if="!msgs.length && !loading" class="empty">
              <div class="empty-art" v-html="art.chat"></div>
              <div class="empty-title">这个会话没有消息</div>
              <div class="empty-desc">可能是空会话，或筛选条件太窄。</div>
            </div>

            <template v-for="g in groups" :key="g.key">
              <div class="day-sep"><span>{{ g.day }}</span></div>
              <div v-for="m in g.items" :key="m.id" class="msg" :class="{out: m.direction==='out'}">
                <div class="avatar avatar-sm" :style="m._merged ? {opacity: 0} : {}">
                  <span>{{ initial(m.senderName || (m.direction==='out' ? '我' : '?')) }}</span>
                </div>
                <div class="msg-body">
                  <div class="msg-sender" v-if="isGroup && m.direction!=='out'">
                    {{ senderLabel(m) }}
                  </div>
                  <div class="bubble" :class="bubbleClass(m)">
                    <!-- 图片 -->
                    <template v-if="m.kind==='image'">
                      <!-- 气泡里只加载缩略图；点开才请求原图 -->
                      <img v-if="m.imgMd5 && !m.imgFailed"
                           :src="M.mediaUrl(m.imgMd5, true)"
                           @error="m.imgFailed = true"
                           @click="openImage(m)"
                           loading="lazy" />
                      <span v-else class="media-ph">🖼 图片加载失败</span>
                    </template>
                    <!-- 语音 -->
                    <template v-else-if="m.kind==='voice'">
                      <span>🎙 {{ voiceText(m) }}</span>
                    </template>
                    <!-- 视频 / 文件 -->
                    <span v-else-if="m.kind==='video'" class="media-ph">🎬 视频</span>
                    <span v-else-if="m.kind==='file'" class="media-ph">
                      📎 {{ m.title || m.fileName || '文件' }}
                    </span>
                    <span v-else-if="m.kind==='emoji'" class="media-ph">😀 表情</span>
                    <span v-else-if="m.kind==='system'" class="muted">{{ m.content }}</span>
                    <!-- 兜底：后端已把已知 XML 翻成人话，但万一有漏网的，
                         也不把原始标签（含 roomid/token 等内部字段）摆到界面上 -->
                    <span v-else-if="looksLikeXml(m.content)" class="muted">[不支持的消息类型]</span>
                    <span v-else>{{ m.content }}</span>
                  </div>
                  <div class="msg-meta">
                    <span>{{ fmtTime(m.createTime) }}</span>
                    <span v-if="m.kind==='voice'" class="tiny">{{ (m.voiceMs/1000).toFixed(1) }}s</span>
                    <button v-if="m.kind==='text' && m.content" class="copy-btn"
                            title="复制原文" @click="copyMsg(m)">复制</button>
                  </div>
                </div>
              </div>
            </template>
            <div v-if="loading" style="padding:16px">
              <div v-for="i in 5" class="skeleton" :key="i"
                   style="height:44px;margin-bottom:10px"></div>
            </div>
          </div>
        </template>

        <div v-else class="empty" style="height:100%">
          <div class="empty-art" v-html="art.folder"></div>
          <div class="empty-title">选择一个会话开始浏览</div>
          <div class="empty-desc">
            左侧是你的全部会话。支持按类型/方向/日期筛选，也支持全库关键词搜索。
          </div>
        </div>
      </div>

      <!-- 全库搜索 -->
      <div v-if="searchOpen" class="overlay" @click.self="searchOpen=false">
        <div class="modal modal-lg">
          <div class="modal-head">
            <div>
              <div class="modal-title">全库搜索</div>
              <div class="modal-sub">在全部会话里查找包含关键词的消息</div>
            </div>
          </div>
          <div class="modal-body">
            <div class="row">
              <input class="input" v-model="sq" @keyup.enter="doSearch"
                     placeholder="输入关键词后回车…" autofocus />
              <button class="btn btn-primary" @click="doSearch" :disabled="searching">
                <span v-if="searching" class="spinner"></span>搜索
              </button>
            </div>
            <div class="divider"></div>
            <div v-if="sResults.length" style="max-height:52vh;overflow-y:auto">
              <div v-for="(r,i) in sResults" :key="i" class="session-item"
                   style="border:1px solid var(--border);border-radius:10px;margin-bottom:8px"
                   @click="jump(r)">
                <div class="session-meta">
                  <div class="session-name">{{ r.sessionName || r.sessionId }}</div>
                  <div class="session-preview" v-html="highlight(r.content, sq)"></div>
                  <div class="tiny muted" style="margin-top:4px">{{ fmtTime(r.createTime) }}</div>
                </div>
              </div>
            </div>
            <div v-else-if="searched" class="empty"><div class="empty-title">没有找到</div></div>
          </div>
          <div class="modal-foot">
            <button class="btn" @click="searchOpen=false">关闭</button>
          </div>
        </div>
      </div>

      <!-- 灯箱：缩略图打底 → 原图加载 → 失败重试 -->
      <lightbox v-if="lightbox" :item="lightbox" @close="lightbox = null" />
    </div>`,

    data() {
      return {
        kw: "", filter: "all", loading: false, sessions: [],
        cur: null, curId: "", msgs: [], loadingMore: false, hasMore: false, offset: 0,
        total: null,
        fType: "", fDir: "", fFrom: "", fTo: "",
        searchOpen: false, sq: "", sResults: [], searched: false, searching: false,
        lightbox: null,
        art: ART,
      };
    },
    computed: {
      // 模板里要用 M.mediaUrl()，必须把 M 挂到组件实例上。
      // 注意：不能写进 methods —— 这个 Vue 构建里 methods 只绑定函数，
      // 挂对象进去模板取到的仍是 undefined（真机隔离实验验证过）。
      // data / computed / globalProperties 才能让模板读到对象。
      M() { return M; },
      shown() {
        const k = this.kw.trim().toLowerCase();
        return this.sessions.filter((s) => {
          if (this.filter !== "all" && s.type !== this.filter) return false;
          if (!k) return true;
          return (s.displayName || "").toLowerCase().includes(k)
            || (s.summary || "").toLowerCase().includes(k);
        });
      },
      isGroup() { return this.cur && this.cur.type === "group"; },
      groups() {
        // 按天分组 + 同一人 5 分钟内的连续消息归为一组（视觉上更接近真实聊天）
        const out = [];
        let lastDay = "", lastSender = "", lastTs = 0, cur = null;
        for (const m of this.msgs) {
          const day = M.fmtDay(m.createTime);
          if (day !== lastDay) {
            cur = { key: day + Math.random(), day, items: [] };
            out.push(cur);
            lastDay = day; lastSender = ""; lastTs = 0;
          }
          const near = m.senderId === lastSender && Math.abs(m.createTime - lastTs) < 300;
          m._merged = near;
          cur.items.push(m);
          lastSender = m.senderId; lastTs = m.createTime;
        }
        return out;
      },
    },
    methods: {
      ...{ fmtTime: M.fmtTime, fmtNum: M.fmtNum, initial: M.initial, highlight: M.highlight },
      avatarBg(s) { return M.avatarColor(s.displayName); },
      /** 判断这段文本是不是原始 XML —— 用于兜底，避免把内部字段摆到界面上 */
      looksLikeXml(t) {
        const s = (t || "").trimStart();
        return s.startsWith("<") || s.startsWith("<?xml");
      },
      hl(t) { return M.highlight(t, this.kw); },
      typeLabel(s) {
        return { private: "单聊", group: "群聊", mp: "公众号" }[s.type] || "会话";
      },
      bubbleClass(m) {
        if (m.kind === "image") return "bubble-image";
        if (m.direction !== "out" && !m._merged) return "tail-in";
        return "";
      },
      voiceText(m) {
        const t = (m.voiceText || "").trim();
        return t || `[语音 ${Math.round((m.voiceMs || 0) / 1000)} 秒]`;
      },
      openImage(m) {
        this.lightbox = {
          thumb: M.mediaUrl(m.imgMd5, true),
          full: M.mediaUrl(m.imgMd5, false),
          note: "滚轮缩放 · 拖拽移动 · Esc 关闭",
        };
      },
      async copyMsg(m) {
        try {
          await navigator.clipboard.writeText(m.content || "");
          M.toast.ok("已复制");
        } catch (e) { M.toast.err("复制失败：" + e.message); }
      },
      /** 群成员显示名兜底：拿不到昵称时别甩一串 wxid 在用户脸上 */
      senderLabel(m) {
        const n = (m.senderName || "").trim();
        if (n) return n;
        const id = m.senderId || "";
        return id.startsWith("wxid_") ? "群成员" : (id || "群成员");
      },
      async load() {
        this.loading = true;
        try {
          const d = await M.api.get("/api/sessions", { limit: 500 });
          // 后端字段 lastMessage → 展示用 summary
          this.sessions = (d.sessions || []).map((s) => ({ ...s, summary: s.lastMessage }));
        } catch (e) {
          M.toast.err("会话列表加载失败：" + e.message);
        } finally { this.loading = false; }
      },
      async pick(s) {
        this.cur = s; this.curId = s.sessionId;
        this.msgs = []; this.offset = 0; this.hasMore = false;
        await this.fetchMessages();
        this.$nextTick(() => { const el = this.$refs.scroller; if (el) el.scrollTop = el.scrollHeight; });
        const u = new URL(location.href);
        u.hash = "#/browse?session=" + encodeURIComponent(s.sessionId);
        history.replaceState(null, "", u);
      },
      async reload() { if (this.cur) { this.msgs = []; this.offset = 0; await this.fetchMessages(); } },
      _query() {
        let q = "";
        if (this.fType) q += `&type=${this.fType}`;
        if (this.fDir) q += `&direction=${this.fDir}`;
        if (this.fFrom) q += `&from=${this.fFrom}`;
        if (this.fTo) q += `&to=${this.fTo}`;
        return q;
      },
      async fetchMessages() {
        if (!this.cur) return;
        this.loading = true;
        try {
          const d = await M.api.get("/api/messages", {
            session: this.cur.sessionId, limit: 60, offset: this.offset,
            ...(this.fType ? { type: this.fType } : {}),
            ...(this.fDir ? { direction: this.fDir } : {}),
            ...(this.fFrom ? { from: this.fFrom } : {}),
            ...(this.fTo ? { to: this.fTo } : {}),
          });
          // 后端已给稳定的 id；这里只补前端私有状态（图片失败标记）
          const list = (d.messages || []).map((m) => ({ ...m, imgFailed: false }));
          this.msgs = this.offset ? list.concat(this.msgs) : list;
          this.hasMore = !!d.hasMore;
          this.total = d.total;
        } catch (e) {
          M.toast.err("消息加载失败：" + e.message);
        } finally { this.loading = false; }
      },
      async loadMore() {
        if (this.loadingMore || !this.cur) return;
        const el = this.$refs.scroller;
        const before = el ? el.scrollHeight : 0;
        this.loadingMore = true;
        this.offset += 60;
        await this.fetchMessages();
        this.$nextTick(() => {
          if (el) el.scrollTop = el.scrollHeight - before + el.scrollTop;
        });
        this.loadingMore = false;
      },
      onScroll(e) {
        if (e.target.scrollTop < 40 && this.hasMore && !this.loadingMore) this.loadMore();
      },
      openSearch() { this.searchOpen = true; this.sq = ""; this.sResults = []; this.searched = false; },
      async doSearch() {
        if (!this.sq.trim()) return;
        this.searching = true; this.searched = false;
        try {
          const d = await M.api.get("/api/search", { q: this.sq.trim(), limit: 80 });
          this.sResults = d.messages || [];
          this.searched = true;
          if (!this.sResults.length) M.toast("没有找到相关消息");
        } catch (e) { M.toast.err("搜索失败：" + e.message); }
        finally { this.searching = false; }
      },
      jump(r) {
        this.searchOpen = false;
        const s = this.sessions.find((x) => x.sessionId === r.sessionId);
        if (s) { this.pick(s); M.toast.ok(`已跳转到「${s.displayName}」`); }
        else M.toast.err("该会话不在列表中，请先在左侧找到它");
      },
    },
    async mounted() {
      await this.load();
      // 从导航「全库搜索」进入：直接弹搜索框，不等用户再找按钮
      if (this.searchOnMount) this.openSearch();
      const h = location.hash.match(/session=([^&]+)/);
      if (h) {
        const id = decodeURIComponent(h[1]);
        const s = this.sessions.find((x) => x.sessionId === id);
        if (s) this.pick(s);
      }
    },
  };

  global.MosuViews = global.MosuViews || {};
  global.MosuViews.Browse = Browse;
})(window);
