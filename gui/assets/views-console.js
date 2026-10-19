/* ==========================================================================
   页面 · 控制台（数字分身对话调试 + 通道状态 + 运行流水）
   ========================================================================== */
(function (global) {
  "use strict";
  const M = global.Mosu;

  const Console = {
    template: `
    <div style="height:100%;display:flex;flex-direction:column;padding:20px;gap:16px;overflow:hidden">
      <div class="grid grid-4" style="flex:none">
        <div class="stat-card">
          <div class="stat-value" style="font-size:20px">
            {{ status && status.bot ? fmtNum(status.bot.corpus) : '—' }}
          </div>
          <div class="stat-label">风格语料条数</div>
        </div>
        <div class="stat-card">
          <div class="stat-value" style="font-size:20px">
            <span class="badge" :class="wxOk ? 'badge-success' : ''">
              {{ wxOk ? '已连接' : '未连接' }}
            </span>
          </div>
          <div class="stat-label">微信通道</div>
        </div>
        <div class="stat-card">
          <div class="stat-value" style="font-size:20px">
            <span class="badge" :class="alive ? 'badge-success' : 'badge-warn'">
              {{ alive ? '活着' : '无心跳' }}
            </span>
          </div>
          <div class="stat-label">服务心跳</div>
        </div>
        <div class="stat-card">
          <div class="stat-value" style="font-size:14px;line-height:1.4">
            {{ llm && llm.hasKey ? (llm.model || '已配置') : '未配置大模型' }}
          </div>
          <div class="stat-label">对话模型</div>
        </div>
      </div>

      <div class="console" style="flex:1;min-height:0">
        <div class="card" style="display:flex;flex-direction:column;overflow:hidden">
          <div class="card-title">
            试着和「你」聊一句
            <div class="spacer"></div>
            <button class="btn btn-sm btn-ghost" @click="log=[]">清空</button>
          </div>
          <div class="chat-log" ref="logEl" style="flex:1;overflow-y:auto">
            <div v-if="!log.length" class="empty" style="padding:40px 20px">
              <div class="empty-icon">🗣</div>
              <div class="empty-title">问一句试试</div>
              <div class="empty-desc">
                用你的聊天语料做风格锚点生成回复。需要在设置里填好大模型 URL 与 API Key。
              </div>
            </div>
            <div v-for="(m,i) in log" :key="i" class="log-line" :class="{out: m.role==='me'}">
              <div class="log-bubble">
                <div style="white-space:pre-wrap">{{ m.text }}</div>
                <div class="tiny muted" style="margin-top:6px">
                  {{ m.role==='me' ? '我' : '分身' }}
                  <span v-if="m.meta"> · {{ m.meta }}</span>
                </div>
              </div>
            </div>
          </div>
          <div class="composer">
            <input class="input" v-model="q" @keyup.enter="send" placeholder="输入一句话，回车发送…" />
            <button class="btn btn-primary" @click="send" :disabled="!q.trim() || sending">
              <span v-if="sending" class="spinner"></span>发送
            </button>
          </div>
        </div>

        <div class="card" style="overflow:hidden;display:flex;flex-direction:column">
          <div class="card-title">运行流水</div>
          <div style="flex:1;overflow-y:auto" class="mono">
            <div v-for="(e,i) in events" :key="i"
                 style="padding:6px 0;border-bottom:1px solid var(--border)">
              <span class="muted">{{ e.time || '' }}</span>
              {{ e.text || e.message || e }}
            </div>
            <div v-if="!events.length" class="tiny muted">暂无</div>
          </div>
          <div class="divider"></div>
          <dl class="kv tiny">
            <dt>通道账号</dt><dd class="mono">{{ wxAccount || '—' }}</dd>
            <dt>服务地址</dt><dd class="mono">{{ wxBase || '—' }}</dd>
            <dt>游标</dt><dd>{{ wxCursor ? '已保存' : '从头开始' }}</dd>
          </dl>
        </div>
      </div>
    </div>`,
    data() { return { q: "", log: [], sending: false, events: [] }; },
    computed: {
      status() { return M.store.status; },
      llm() { return M.store.llm; },
      wxOk() { return !!(this.status && this.status.wechat && this.status.wechat.logged_in); },
      alive() { return !!(this.status && this.status.service && this.status.service.alive); },
      wxAccount() {
        const w = this.status && this.status.wechat;
        return w ? (w.user || w.account || "") : "";
      },
      wxBase() { const w = this.status && this.status.wechat; return w ? w.base_url : ""; },
      wxCursor() { const w = this.status && this.status.wechat; return w ? w.cursor : false; },
    },
    methods: {
      ...{ fmtNum: M.fmtNum },
      async send() {
        const text = this.q.trim();
        if (!text || this.sending) return;
        this.log.push({ role: "me", text });
        this.q = ""; this.sending = true;
        const t0 = performance.now();
        try {
          const d = await M.api.post("/api/chat", { message: text });
          const reply = (d && (d.reply || d.text)) || "（没有返回内容）";
          this.log.push({
            role: "bot", text: reply,
            meta: Math.round(performance.now() - t0) + "ms"
              + (d && d.cached ? " · 缓存" : "")
              + (d && d.model ? " · " + d.model : ""),
          });
        } catch (e) {
          this.log.push({ role: "bot", text: "出错了：" + e.message, meta: "" });
        } finally {
          this.sending = false;
          this.$nextTick(() => {
            const el = this.$refs.logEl;
            if (el) el.scrollTop = el.scrollHeight;
          });
          this.loadLog();
        }
      },
      async loadLog() {
        try {
          const d = await M.api.get("/api/log", { n: 40 });
          this.events = d.events || [];
        } catch (e) { /* 忽略 */ }
      },
    },
    mounted() {
      this.loadLog();
      clearInterval(this._t);
      this._t = setInterval(() => { M.refreshStatus(); this.loadLog(); }, 5000);
    },
    beforeUnmount() { clearInterval(this._t); },
  };

  global.MosuViews = global.MosuViews || {};
  global.MosuViews.Console = Console;
})(window);
