/* ==========================================================================
   页面 · 控制台（数字分身对话调试 + 通道状态 + 运行流水）
   交互要点：
   - 等待期显示「思考点」，而不是干等
   - 回复以打字机方式流出（总时长封顶，长回复不会让人等到睡着）
   - 空态给可点击的建议问题，别让用户对着输入框发呆
   ========================================================================== */
(function (global) {
  "use strict";
  const M = global.Mosu;

  /* 空态插画：细线风，纯 SVG，不依赖位图与网络 */
  const EmptyArt = `
  <svg viewBox="0 0 132 96" fill="none" stroke="currentColor" stroke-width="1.6"
       stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <rect x="10" y="16" width="62" height="40" rx="10" opacity=".55"/>
    <path d="M24 34h34M24 43h22" opacity=".4"/>
    <rect x="62" y="40" width="60" height="38" rx="10"/>
    <path d="M74 56h36M74 65h24" opacity=".55"/>
    <path d="M62 66l-8 8v-7" opacity=".55"/>
    <circle cx="104" cy="20" r="2.4" opacity=".5"/>
    <circle cx="114" cy="26" r="1.8" opacity=".32"/>
  </svg>`;

  const SUGGESTIONS = [
    "在吗",
    "最近怎么样",
    "帮我总结一下最近的聊天",
    "我平时说话是什么风格",
  ];

  const Console = {
    template: `
    <div style="height:100%;display:flex;flex-direction:column;padding:20px;gap:16px;overflow:hidden">
      <div class="grid grid-4 rise" style="flex:none">
        <div class="tile">
          <div class="tile-head">风格语料</div>
          <div class="tile-value" style="font-size:24px">
            {{ status && status.bot ? fmtNum(status.bot.corpus) : '—' }}
          </div>
          <div class="tile-foot">用于锚定说话方式的样本条数</div>
        </div>
        <div class="tile">
          <div class="tile-head">微信通道</div>
          <div class="tile-value" style="font-size:24px">
            <span :class="wxOk ? 'trend trend-down' : 'trend trend-flat'">{{ wxOk ? '已连接' : '未连接' }}</span>
          </div>
          <div class="tile-foot">{{ wxAccount ? ('账号 ' + wxAccount) : '尚未登录通道' }}</div>
        </div>
        <div class="tile">
          <div class="tile-head">服务心跳</div>
          <div class="tile-value" style="font-size:24px">
            <span :class="beatClass">{{ beatLabel }}</span>
          </div>
          <div class="tile-foot">{{ beatText }}</div>
        </div>
        <div class="tile">
          <div class="tile-head">对话模型</div>
          <div class="tile-value" style="font-size:16px;line-height:1.35">
            {{ llm && llm.hasKey ? (llm.model || '已配置') : '未配置' }}
          </div>
          <div class="tile-foot">
            <a v-if="!(llm && llm.hasKey)" href="#/settings" class="suggest" style="height:22px;padding:0 10px">
              去填写 URL 与 API Key
            </a>
            <span v-else>点击右上角可随时更换</span>
          </div>
        </div>
      </div>

      <div class="console" style="flex:1;min-height:0">
        <div class="card" style="display:flex;flex-direction:column;overflow:hidden">
          <div class="card-title">
            试着和「你」聊一句
            <div class="spacer"></div>
            <span class="tiny muted" v-if="persona">{{ curPersona.tagline }}</span>
            <button class="btn btn-sm btn-ghost" @click="log=[]" v-if="log.length">清空</button>
          </div>

          <!-- 角色切换：AI 能扮演谁，决定它说什么、能不能引用数据 -->
          <div class="persona-bar">
            <button v-for="p in personas" :key="p.id" class="persona-chip"
                    :class="{active: persona === p.id}"
                    :title="p.intent" @click="pickPersona(p.id)">
              {{ p.name }}
              <span class="tiny">{{ p.tagline }}</span>
            </button>
          </div>

          <div class="chat-log" ref="logEl" style="flex:1;overflow-y:auto">
            <div v-if="!log.length && !sending" class="empty" style="padding:36px 20px">
              <div class="empty-art" v-html="art"></div>
              <div class="empty-title">问一句试试</div>
              <div class="empty-desc">
                用你的聊天语料做风格锚点生成回复。需要先在设置里填好大模型 URL 与 API Key。
              </div>
              <div class="empty-actions">
                <button v-for="s in suggestions" :key="s" class="suggest" @click="ask(s)">{{ s }}</button>
              </div>
            </div>

            <div v-for="(m,i) in log" :key="i" class="log-line rise" :class="{out: m.role==='me'}">
              <div class="log-bubble">
                <div style="white-space:pre-wrap" v-html="render(m)"></div>
                <div class="tiny muted" style="margin-top:6px">
                  {{ m.role==='me' ? '我' : '分身' }}
                  <span v-if="m.meta"> · {{ m.meta }}</span>
                </div>
              </div>
            </div>

            <div v-if="sending" class="log-line">
              <div class="thinking"><i></i><i></i><i></i></div>
            </div>
          </div>

          <div class="composer">
            <input class="input" v-model="q" @keyup.enter="send"
                   placeholder="输入一句话，回车发送…" :disabled="sending" />
            <button class="btn btn-primary btn-shine" @click="send"
                    :class="{'btn-loading': sending}" :disabled="!q.trim() || sending">
              {{ sending ? '发送中' : '发送' }}
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
    data() {
      return { q: "", log: [], sending: false, events: [], art: EmptyArt,
               suggestions: SUGGESTIONS, personas: [], persona: "" };
    },
    computed: {
      curPersona() {
        return this.personas.find((p) => p.id === this.persona)
          || { name: "模仿者", tagline: "", intent: "" };
      },
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
      beatText() {
        const s = this.status && this.status.service;
        if (!s || s.last_seen === null || s.last_seen === undefined) {
          return "从未收到心跳——通道服务可能没启动";
        }
        const d = s.detail || {};
        const bits = [`${Math.round(s.last_seen)} 秒前`];
        // 先判存活，再判 phase。
        // 反过来的话，一个已经断线 15 小时的进程最后 phase 是 polling，
        // 就会显示成「正常等待消息」，把故障说成正常——这是实测踩到的。
        if (!s.alive) {
          bits.push("心跳已过期：进程可能已退出，需要重新拉起");
        } else if (d.phase === "retry" && d.fails) {
          bits.push(`接口连续失败 ${d.fails} 次，正在退避重试`);
        } else if (d.phase === "polling") {
          // 长轮询本来就是挂起等消息，几十秒不更新是正常的——
          // 不说明这点的话，用户会以为它卡住了
          bits.push("正常等待消息（长轮询会挂起几十秒）");
        } else if (d.phase === "ok" && d.got) {
          bits.push(`刚收到 ${d.got} 条`);
        }
        if (d.pid) bits.push(`PID ${d.pid}`);
        return bits.join(" · ");
      },
      /**
       * 心跳状态文案。原来只有「活着 / 无心跳」两种，
       * 但「无心跳」可能意味着三件完全不同的事：
       *   进程真死了（要拉起） / 卡在某次请求里（要杀掉重来） / 从未启动过
       * 三者处理方式不同，所以必须分开说。
       */
      beatLabel() {
        const s = this.status && this.status.service;
        if (!s) return "无数据";
        if (s.last_seen === null || s.last_seen === undefined) return "未启动";
        if (!s.alive) return "已断开";
        const d = s.detail || {};
        if (d.phase === "retry") return "重试中";
        return "正常";      // ok / polling / 老版本无 phase，都算正常
      },
      beatClass() {
        const s = this.status && this.status.service;
        const d = (s && s.detail) || {};
        if (!s || s.last_seen === null || s.last_seen === undefined) {
          return "trend trend-up";        // 未启动 = 警示色
        }
        if (!s.alive) return "trend trend-up";          // 断开 = 警示色
        if (d.phase === "retry") return "trend trend-up"; // 重试中 = 警示色
        return "trend trend-down";
      },
    },
    methods: {
      ...{ fmtNum: M.fmtNum },
      render(m) {
        const t = m.role === "bot" && m.typing ? m.shown : m.text;
        const esc = String(t || "").replace(/[&<>"]/g, (c) =>
          ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
        return esc + (m.typing ? '<span class="caret"></span>' : "");
      },
      ask(s) { this.q = s; this.send(); },
      /** 打字机：总时长封顶 900ms，按字数算步进，长回复也不会拖成慢动作 */
      typeOut(item, full) {
        const dur = Math.min(900, Math.max(200, full.length * 12));
        const step = Math.max(2, Math.ceil(full.length / (dur / 16)));
        item.shown = "";
        item.typing = true;
        clearInterval(item._timer);
        item._timer = setInterval(() => {
          item.shown = full.slice(0, (item.shown || "").length + step);
          if ((item.shown || "").length >= full.length) {
            item.shown = full;
            item.typing = false;
            clearInterval(item._timer);
            this.scroll();
          }
        }, 16);
      },
      scroll() {
        this.$nextTick(() => {
          const el = this.$refs.logEl;
          if (el) el.scrollTop = el.scrollHeight;
        });
      },
      async send() {
        const text = this.q.trim();
        if (!text || this.sending) return;
        this.log.push({ role: "me", text });
        this.q = ""; this.sending = true; this.scroll();
        const t0 = performance.now();
        try {
          const d = await M.api.post("/api/chat", { message: text });
          if (d && d.persona) this.persona = d.persona;
          const reply = (d && (d.reply || d.text)) || "（没有返回内容）";
          const item = {
            role: "bot", text: reply, shown: "", typing: true,
            meta: (this.persona ? this.curPersona.name + " · " : "")
              + Math.round(performance.now() - t0) + "ms"
              + (d && d.cached ? " · 缓存" : "")
              + (d && d.model ? " · " + d.model : ""),
          };
          this.log.push(item);
          this.typeOut(item, reply);
        } catch (e) {
          this.log.push({ role: "bot", text: "出错了：" + e.message, meta: "" });
        } finally {
          this.sending = false;
          this.scroll();
          this.loadLog();
        }
      },
      async pickPersona(id) {
        if (this.persona === id) return;
        try {
          const d = await M.api.post("/api/persona/set", { persona: id });
          this.persona = d.current || id;
          M.toast.ok("已切到「" + (d.name || id) + "」");
        } catch (e) { M.toast.err("切换失败：" + e.message); }
      },
      async loadLog() {
        try {
          const d = await M.api.get("/api/log", { n: 40 });
          this.events = d.events || [];
        } catch (e) { /* 流水是辅助信息，失败不影响主流程 */ }
      },
    },
    mounted() {
      this.loadLog();
      M.api.get("/api/personas").then((d) => {
        this.personas = d.personas || [];
        this.persona = d.current || "";
      }).catch(() => {});
      clearInterval(this._t);
      this._t = setInterval(() => { M.refreshStatus(); this.loadLog(); }, 5000);
    },
    beforeUnmount() {
      clearInterval(this._t);
      this.log.forEach((m) => clearInterval(m._timer));
    },
  };

  global.MosuViews = global.MosuViews || {};
  global.MosuViews.Console = Console;
})(window);
