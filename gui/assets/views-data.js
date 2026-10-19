/* ==========================================================================
   页面 · 朋友圈 / 导出 / 联系人
   ========================================================================== */
(function (global) {
  "use strict";
  const M = global.Mosu;

  /* ---------------------------------------------------------------- 朋友圈 */
  const Moments = {
    template: `
    <div class="page">
      <div class="row" style="margin-bottom:16px">
        <div>
          <div class="topbar-title" style="font-size:16px">朋友圈</div>
          <div class="tiny muted">共 {{ total }} 条 · 图片走远程地址，失效会显示占位</div>
        </div>
        <div class="spacer"></div>
        <input class="input" style="width:200px" v-model="user" placeholder="按作者 wxid 过滤"
               @keyup.enter="reload" />
        <button class="btn btn-sm" @click="reload">刷新</button>
      </div>

      <div v-if="unsupported" class="empty">
        <div class="empty-icon">🌐</div>
        <div class="empty-title">本机没有朋友圈数据</div>
        <div class="empty-desc">
          没找到 sns/sns.db。微信可能没有开启朋友圈本地缓存，或该账号从未刷过朋友圈。
        </div>
      </div>
      <div v-else-if="loading" class="moments">
        <div v-for="i in 6" class="skeleton" :key="i" style="height:180px;margin-bottom:16px"></div>
      </div>
      <div v-else-if="!items.length" class="empty">
        <div class="empty-icon">🌐</div>
        <div class="empty-title">没有朋友圈内容</div>
      </div>
      <template v-else>
        <div class="moments">
          <div v-for="e in items" :key="e.tid || e.id" class="moment-card">
            <div class="moment-head">
              <div class="avatar avatar-sm">
                <span>{{ initial(e.author && e.author.displayName) }}</span>
              </div>
              <div style="min-width:0">
                <div class="session-name">{{ (e.author && e.author.displayName) || '未知' }}</div>
                <div class="tiny muted">{{ fmtTime(e.createTime) }}</div>
              </div>
            </div>
            <div class="moment-text">{{ e.contentText }}</div>
            <div v-if="imgList(e).length" class="moment-imgs">
              <div v-for="(m,i) in imgList(e)" :key="i" class="moment-img-cell">
                <img :src="m.thumb + (m.bust || '')" loading="lazy"
                     @click="openImg(m)" @error="onImgErr(m, i)" />
                <div v-if="m.failed" class="moment-img-fallback">
                  <span>图片不可用</span>
                  <button class="btn btn-sm" @click.stop="retryImg(m)">重试</button>
                </div>
              </div>
            </div>
            <div class="moment-foot">
              <span>👍 {{ e.likes || 0 }}</span>
              <span>💬 {{ e.comments || 0 }}</span>
              <span v-if="e.likeUsers && e.likeUsers.length" class="truncate">
                {{ e.likeUsers.slice(0,3).join('、') }}
              </span>
            </div>
            <div v-if="e.commentList && e.commentList.length" style="margin-top:8px">
              <div v-for="(c,i) in e.commentList.slice(0,3)" :key="i" class="tiny muted">
                {{ (c.nickname || c.username || '') + '：' + (c.content || '') }}
              </div>
            </div>
          </div>
        </div>
        <div style="text-align:center;padding:16px">
          <button class="btn" v-if="hasMore" @click="more" :disabled="loadingMore">
            <span v-if="loadingMore" class="spinner"></span>加载更多
          </button>
          <div v-else class="tiny muted">已经到底了</div>
        </div>
      </template>

      <lightbox v-if="lightbox" :item="lightbox" @close="lightbox = null" />
    </div>`,
    components: { Lightbox: global.MosuViews.Lightbox },
    data() {
      return { items: [], total: 0, hasMore: false, loading: true, loadingMore: false,
               unsupported: false, user: "", lightbox: null };
    },
    methods: {
      ...{ fmtTime: M.fmtTime, initial: M.initial },
      async fetch_(offset) {
        const d = await M.api.get("/api/moments", {
          limit: 30, offset, ...(this.user ? { user: this.user } : {}) });
        return d;
      },
      async reload() {
        this.loading = true;
        try {
          const d = await this.fetch_(0);
          this.items = d.entries || [];
          this.total = d.total || 0;
          this.hasMore = !!d.hasMore;
          this.unsupported = !!d.unsupported;
        } catch (e) { M.toast.err("加载失败：" + e.message); }
        finally { this.loading = false; }
      },
      async more() {
        this.loadingMore = true;
        try {
          const d = await this.fetch_(this.items.length);
          this.items = this.items.concat(d.entries || []);
          this.hasMore = !!d.hasMore;
        } catch (e) { M.toast.err(e.message); }
        finally { this.loadingMore = false; }
      },
      /* 图片列表：本地库优先（可靠、可拿到原图），没有才用微信 CDN 远程地址。
         列表只取缩略图，点开时才请求原图 —— 首屏因此不会拖着一堆大图。 */
      imgList(e) {
        const md5 = e.mediaMd5 || [];
        const loc = e.mediaLocal || [];
        const urls = e.mediaUrls || [];
        const out = [];
        for (let i = 0; i < Math.min(urls.length, 9); i++) {
          if (md5[i] && loc[i]) {
            out.push({ thumb: M.mediaUrl(md5[i], true), full: M.mediaUrl(md5[i], false),
                       local: true, bust: "" });
          } else {
            out.push({ thumb: urls[i], full: urls[i], local: false, bust: "" });
          }
        }
        return out;
      },
      openImg(m) { this.lightbox = { ...m, note: m.local ? "本地原图" : "远程原图（微信 CDN）" }; },
      onImgErr(m) { m.failed = true; },
      retryImg(m) { m.failed = false; m.bust = (m.bust ? "" : "&") + "_=" + Date.now(); },
    },
    mounted() { this.reload(); },
  };

  /* ---------------------------------------------------------------- 导出 */
  const ExportView = {
    template: `
    <div class="page">
      <div class="grid grid-2">
        <div class="card">
          <div class="card-title">单会话导出</div>
          <div class="field">
            <label class="field-label">会话</label>
            <select class="select" v-model="sess">
              <option value="">选择一个会话…</option>
              <option v-for="s in sessions" :key="s.sessionId" :value="s.sessionId">
                {{ s.displayName }}（{{ fmtNum(s.messageCount) }} 条）
              </option>
            </select>
          </div>
          <div class="field">
            <label class="field-label">格式</label>
            <div class="row">
              <button class="chip" v-for="f in formats" :key="f.v"
                      :class="{active: fmt===f.v}" @click="fmt=f.v">
                {{ f.label }}
              </button>
            </div>
            <div class="field-hint">{{ fmtHint }}</div>
          </div>
          <div class="row">
            <input class="input" style="width:140px" v-model="from" placeholder="起（可选）"
                   onfocus="this.type='date'" />
            <input class="input" style="width:140px" v-model="to" placeholder="止（可选）"
                   onfocus="this.type='date'" />
            <div class="spacer"></div>
            <button class="btn btn-primary" @click="run" :disabled="!sess || !!task">
              <span v-if="task" class="spinner"></span>开始导出
            </button>
          </div>
        </div>

        <div class="card">
          <div class="card-title">批量导出（前 N 个会话）</div>
          <div class="field">
            <label class="field-label">会话数量（1~30）</label>
            <input class="input" type="number" min="1" max="30" v-model.number="topN" />
            <div class="field-hint">
              按消息量取前 N 个。全量导出会解密所有媒体，很慢很大（本机实测 3
              个会话 16 万条 ≈ 1.2GB / 4 分钟）。
            </div>
          </div>
          <button class="btn" @click="runAll" :disabled="!!task">
            <span v-if="task" class="spinner"></span>开始批量导出
          </button>
        </div>
      </div>

      <div v-if="task" class="card" style="margin-top:16px">
        <div class="card-title">
          正在导出：{{ task.session || '批量' }}
          <div class="spacer"></div>
          <span class="badge badge-brand">{{ task.status }}</span>
        </div>
        <div class="progress"><div class="progress-bar indeterminate"></div></div>
        <div class="tiny muted" style="margin-top:8px">
          已耗时 {{ elapsed }}s · 大导出需要几分钟，可以离开这一页做别的。
        </div>
      </div>

      <div v-if="done" class="card" style="margin-top:16px">
        <div class="card-title">✅ 导出完成</div>
        <dl class="kv">
          <dt>会话</dt><dd>{{ done.session }}</dd>
          <dt>耗时</dt><dd>{{ fmtDur(done.cost) }}</dd>
          <dt>文件</dt><dd>{{ done.filename }}</dd>
        </dl>
        <button class="btn btn-primary" style="margin-top:12px" @click="download">
          下载 {{ done.filename }}
        </button>
      </div>

      <div class="card" style="margin-top:16px">
        <div class="card-title">导出说明</div>
        <ul class="tiny muted" style="padding-left:18px;line-height:1.9">
          <li>HTML：像微信一样的看图/听语音的完整存档（含图片与语音转文字）</li>
          <li>CSV：Excel 可直接打开，表头「时间,方向,发送者,类型,内容」</li>
          <li>JSON：结构化原始数据，方便二次处理</li>
          <li>TXT：纯文本速览</li>
          <li>语音 mp3 需要本机装 ffmpeg；没有则自动降级为转写文字</li>
        </ul>
      </div>
    </div>`,
    data() {
      return {
        sessions: [], sess: "", fmt: "html", topN: 3, from: "", to: "",
        formats: [{ v: "html", label: "HTML 存档" }, { v: "csv", label: "CSV" },
                  { v: "json", label: "JSON" }, { v: "txt", label: "TXT" }],
        task: null, done: null, elapsed: 0, timer: null,
      };
    },
    computed: {
      fmtHint() {
        return {
          html: "完整存档：图片、语音转文字、视频封面都在，推荐。",
          csv: "表格：适合在 Excel 里统计。",
          json: "结构化：适合程序再处理。",
          txt: "纯文本：最小最快。",
        }[this.fmt];
      },
    },
    methods: {
      ...{ fmtNum: M.fmtNum, fmtDur: (ms) => M.fmtDur(ms * 1000) },
      async run() {
        if (!this.sess) return;
        await this.submit({
          scope: "single", session: this.sess, format: this.fmt,
          ...(this.from ? { from: this.from } : {}),
          ...(this.to ? { to: this.to } : {}),
        });
      },
      async runAll() {
        await this.submit({ scope: "all", topN: this.topN,
          ...(this.from ? { from: this.from } : {}),
          ...(this.to ? { to: this.to } : {}) });
      },
      async submit(payload) {
        this.done = null;
        try {
          const d = await M.api.post("/api/export", payload);
          this.task = { id: d.taskId, status: "running", session: d.session };
          this.poll(d.taskId);
        } catch (e) { M.toast.err("导出失败：" + e.message); }
      },
      async poll(tid) {
        this.elapsed = 0;
        clearInterval(this.timer);
        this.timer = setInterval(() => { this.elapsed += 1; }, 1000);
        for (let i = 0; i < 1800; i++) {
          await new Promise((r) => setTimeout(r, 1000));
          let t;
          try { t = await M.api.get("/api/task", { id: tid }); }
          catch (e) { continue; }
          if (t.status === "done") {
            clearInterval(this.timer);
            this.task = null;
            this.done = { ...(t.result || {}), cost: t.cost, session: this.task.session };
            M.toast.ok("导出完成");
            return;
          }
          if (t.status === "error") {
            clearInterval(this.timer);
            this.task = null;
            M.toast.err("导出失败：" + t.error);
            return;
          }
        }
        clearInterval(this.timer);
        this.task = null;
        M.toast.err("导出超时");
      },
      download() {
        if (!this.done) return;
        const url = "/api/export/download?task=" + this.done.id;
        const a = document.createElement("a");
        a.href = url; a.click();
      },
    },
    async mounted() {
      try {
        const d = await M.api.get("/api/sessions", { limit: 500 });
        this.sessions = d.sessions || [];
      } catch (e) { /* 忽略 */ }
      const h = location.hash.match(/session=([^&]+)/);
      if (h) this.sess = decodeURIComponent(h[1]);
    },
    beforeUnmount() { clearInterval(this.timer); },
  };

  /* ---------------------------------------------------------------- 联系人 */
  const Contacts = {
    components: { JsonView: global.MosuViews && global.MosuViews.JsonView },
    template: `
    <div class="page-flat">
      <div class="row" style="padding:0 0 12px">
        <input class="input" style="max-width:280px" v-model="kw" placeholder="搜索备注/昵称/微信 ID…" />
        <button class="chip" :class="{active: sort==='messages'}" @click="sort='messages'">按消息量</button>
        <button class="chip" :class="{active: sort==='name'}" @click="sort='name'">按名称</button>
        <div class="spacer"></div>
        <span class="tiny muted">{{ total }} 位</span>
      </div>
      <div style="display:grid;grid-template-columns:380px 1fr;gap:16px;flex:1;overflow:hidden">
        <div style="overflow-y:auto;border:1px solid var(--border);border-radius:14px;background:var(--bg-surface)">
          <div v-if="loading" style="padding:16px">
            <div v-for="i in 10" class="skeleton" :key="i" style="height:52px;margin-bottom:8px"></div>
          </div>
          <div v-for="c in list" :key="c.wxid" class="session-item"
               :class="{active: cur && cur.wxid===c.wxid}" @click="open(c)">
            <div class="avatar" :style="{background: avatarBg(c)}">
              <span>{{ initial(c.displayName) }}</span>
            </div>
            <div class="session-meta">
              <div class="session-name" v-html="hl(c.displayName)"></div>
              <div class="session-preview">
                {{ typeName(c.type) }}<span v-if="c.messageCount"> · {{ fmtNum(c.messageCount) }} 条</span>
              </div>
            </div>
          </div>
        </div>
        <div style="overflow-y:auto">
          <div v-if="!cur" class="empty">
            <div class="empty-icon">👤</div>
            <div class="empty-title">选择一位联系人</div>
            <div class="empty-desc">右侧是通讯录，按消息量或名称找。选中后展示基本资料与往来统计；群聊会额外给出成员排行。</div>
          </div>
          <div v-else-if="detailLoading" class="skeleton" style="height:300px"></div>
          <div v-else-if="detail">
            <div class="card">
              <div class="card-title">{{ detail.displayName || cur.displayName }}</div>
              <dl class="kv">
                <dt>微信 ID</dt><dd class="mono">{{ cur.wxid }}</dd>
                <dt>微信号</dt><dd>{{ detail.alias || '—' }}</dd>
                <dt>备注</dt><dd>{{ detail.remark || '—' }}</dd>
                <dt>昵称</dt><dd>{{ detail.nickname || '—' }}</dd>
                <dt>类型</dt><dd>{{ typeName(detail.type) }}</dd>
              </dl>
            </div>
            <template v-if="isGroup">
              <div class="card" style="margin-top:16px">
                <div class="card-title">群活跃度</div>
                <div class="card-sub">按数据库中本群保存的全部消息统计。</div>
                <dl class="kv">
                  <dt>消息总数</dt><dd>{{ fmtNum(detail.groupTotal || 0) }}</dd>
                  <dt>上榜成员</dt><dd>{{ fmtNum((detail.groupTop || []).length) }} 人</dd>
                </dl>
              </div>
              <div class="card" style="margin-top:16px">
                <div class="card-title">成员发言排行</div>
                <div class="card-sub">按发言条数排序，你的发言会标出来。</div>
                <div v-for="m in (detail.groupTop || []).slice(0,15)" :key="m.senderId" class="bar-row">
                  <div class="bar-label">
                    {{ m.displayName }}
                    <span v-if="m.isSelf" class="chip" style="margin-left:4px">我</span>
                  </div>
                  <div class="bar-track">
                    <div class="bar-fill" :style="{width: barPct(m.messageCount, topMax)}"></div>
                  </div>
                  <div class="bar-value">{{ fmtNum(m.messageCount) }}</div>
                </div>
                <div v-if="!(detail.groupTop || []).length" class="muted tiny">（无记录）</div>
              </div>
            </template>
            <div class="card" style="margin-top:16px" v-else-if="agg">
              <div class="card-title">往来统计</div>
              <div class="card-sub">按数据库中你们会话的全部消息统计{{ agg.sessions > 1 ? '（合并 ' + agg.sessions + ' 个会话）' : '' }}。</div>
              <dl class="kv">
                <dt>消息总数</dt><dd>{{ fmtNum(agg.total) }}</dd>
                <dt>我发出</dt><dd>{{ fmtNum(agg.sent) }}（{{ share(agg.sent, agg.total) }}）</dd>
                <dt>对方发出</dt><dd>{{ fmtNum(agg.recv) }}（{{ share(agg.recv, agg.total) }}）</dd>
                <dt>文本 / 媒体</dt>
                <dd>{{ fmtNum(agg.text) }} / {{ fmtNum(agg.media) }}</dd>
                <dt>有消息的天数</dt><dd>{{ fmtNum(agg.days) }}</dd>
                <dt>首条消息</dt><dd>{{ agg.first || '—' }}</dd>
                <dt>最近消息</dt><dd>{{ agg.last || '—' }}</dd>
              </dl>
            </div>
            <div class="card" style="margin-top:16px" v-else>
              <div class="card-title">往来统计</div>
              <div class="muted tiny">数据库里没有你们的消息记录（可能只有通讯录条目，或聊天记录未同步到本机）。</div>
            </div>
            <div class="card" style="margin-top:16px" v-if="multiSessions.length">
              <div class="card-title">会话列表</div>
              <div class="card-sub">同一联系人存在多个会话时逐一列出。</div>
              <dl class="kv">
                <template v-for="s in multiSessions" :key="s.sessionId">
                  <dt>{{ s.displayName }}</dt>
                  <dd>{{ fmtNum(s.messageCount) }} 条 · 最近 {{ tsDay(s.lastTime) }}</dd>
                </template>
              </dl>
            </div>
            <div class="card" style="margin-top:16px" v-if="extraKeys.length">
              <json-view :data="extraData" title="其他记录" />
            </div>
          </div>
        </div>
      </div>
    </div>`,
    data() {
      return { list: [], total: 0, kw: "", sort: "messages", loading: true,
               cur: null, detail: null, detailLoading: false };
    },
    computed: {
      isGroup() { return (this.cur && this.cur.type === "group"); },
      topMax() {
        return Math.max(1, ...(this.detail && this.detail.groupTop || [])
          .map((m) => m.messageCount));
      },
      /** 跨会话合并的往来统计（私聊）；无消息记录时为 null */
      agg() {
        const d = this.detail;
        if (!d || this.isGroup) return null;
        const ss = (d.sessions || []).filter((s) => s.stats);
        if (!ss.length) return null;
        const sum = (f) => ss.reduce((a, s) => a + (s.stats[f] || 0), 0);
        const fmtTs = (t) => {
          if (!t) return null;
          const x = new Date(t * 1000);
          const p = (n) => (n < 10 ? "0" + n : n);
          return `${x.getFullYear()}-${p(x.getMonth() + 1)}-${p(x.getDate())} ${p(x.getHours())}:${p(x.getMinutes())}`;
        };
        return {
          sessions: ss.length,
          total: sum("totalMessages"),
          sent: sum("sentMessages"),
          recv: sum("receivedMessages"),
          text: sum("textMessages"),
          media: sum("mediaMessages"),
          days: sum("activeDays"),
          first: ss.map((s) => s.stats.firstMessageText)
            .filter(Boolean).sort()[0] || null,
          last: ss.map((s) => s.stats.lastMessageText)
            .filter(Boolean).sort().pop() || null,
          _fmtTs: fmtTs,
        };
      },
      multiSessions() {
        const d = this.detail;
        if (!d || this.isGroup) return [];
        return (d.sessions || []).length > 1 ? d.sessions : [];
      },
      /** 已有专属呈现的字段，不再进「其他记录」 */
      covered() {
        return new Set(["wxid", "displayName", "type", "remark", "nickname",
          "alias", "sessions", "groupTop", "groupTotal"]);
      },
      extraKeys() {
        if (!this.detail) return [];
        return Object.keys(this.detail).filter((k) => !this.covered.has(k));
      },
      extraData() {
        return Object.fromEntries(this.extraKeys.map((k) => [k, this.detail[k]]));
      },
    },
    methods: {
      ...{ fmtNum: M.fmtNum, initial: M.initial, highlight: M.highlight },
      avatarBg(c) { return M.avatarColor(c.displayName); },
      hl(t) { return M.highlight(t, this.kw); },
      barPct(v, max) { return Math.max(2, (v / Math.max(1, max)) * 100) + "%"; },
      share(part, total) {
        return Math.round((part || 0) / Math.max(1, total || 0) * 1000) / 10 + "%";
      },
      tsDay(t) {
        if (!t) return "—";
        const x = new Date(t * 1000);
        const p = (n) => (n < 10 ? "0" + n : n);
        return `${x.getFullYear()}-${p(x.getMonth() + 1)}-${p(x.getDate())}`;
      },
      typeName(t) {
        return { friend: "好友", private: "好友", group: "群聊", mp: "公众号",
                 former_friend: "已删除（保留聊天记录）" }[t] || t || "—";
      },
      async fetch_() {
        this.loading = true;
        try {
          const d = await M.api.get("/api/contacts", {
            limit: 200, sort: this.sort, ...(this.kw ? { q: this.kw } : {}) });
          this.list = d.contacts || [];
          this.total = d.total || 0;
        } catch (e) { M.toast.err("加载失败：" + e.message); }
        finally { this.loading = false; }
      },
      async open(c) {
        this.cur = c; this.detail = null; this.detailLoading = true;
        location.hash = "#/contacts?wxid=" + encodeURIComponent(c.wxid);
        try {
          this.detail = await M.api.get("/api/contact", { wxid: c.wxid });
        } catch (e) { M.toast.err("详情加载失败：" + e.message); }
        finally { this.detailLoading = false; }
      },
    },
    async mounted() {
      await this.fetch_();
      /* 深链：#/contacts?wxid=xxx 直接打开对应联系人 */
      const h = location.hash.match(/wxid=([^&]+)/);
      if (h) {
        const id = decodeURIComponent(h[1]);
        const c = this.list.find((x) => x.wxid === id) ||
          { wxid: id, displayName: id, type: "" };
        this.open(c);
      }
    },
  };

  global.MosuViews = global.MosuViews || {};
  Object.assign(global.MosuViews, { Moments, ExportView, Contacts });
})(window);
