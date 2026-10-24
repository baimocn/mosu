/* ==========================================================================
   页面 · 个人语料（bot 素材：语料包 + 风格画像）
   对应 CLI：mosu persona --output persona/

   产物落项目 persona/（gitignore）——这个位置是功能性的：数字分身的
   StyleRetriever 写死从这里读，所以不做「保存到」选择，只给下载副本。
   生成是全库扫描（1-2 分钟），走 TaskHub 异步；已有产物时重生成要显式
   确认，防误点。
   ========================================================================== */
(function (global) {
  "use strict";
  const M = global.Mosu;

  const Persona = {
    template: `
    <div class="page">
      <div class="grid grid-4 rise">
        <div class="tile">
          <div class="tile-head">语料条数</div>
          <div class="tile-value">{{ state.corpus ? fmtNum(state.corpus) : '—' }}</div>
          <div class="tile-foot">你本人发出的消息</div>
        </div>
        <div class="tile">
          <div class="tile-head">覆盖会话</div>
          <div class="tile-value">{{ state.sessions ? fmtNum(state.sessions) : '—' }}</div>
          <div class="tile-foot">按会话分段的文本文件</div>
        </div>
        <div class="tile">
          <div class="tile-head">风格画像</div>
          <div class="tile-value" style="font-size:20px">
            {{ state.styleProfile ? '已生成' : '未生成' }}
          </div>
          <div class="tile-foot">style_profile.md</div>
        </div>
        <div class="tile">
          <div class="tile-head">生成时间</div>
          <div class="tile-value" style="font-size:15px">{{ state.mtime || '—' }}</div>
          <div class="tile-foot">重新生成会全库重扫</div>
        </div>
      </div>

      <div class="card" style="margin-top:16px">
        <div class="card-title">{{ state.corpus ? '重新生成语料包' : '生成语料包' }}</div>
        <div class="card-sub">
          全库收集你本人发出的消息，按会话分段存文本，并蒸馏出一份风格画像
          （平均长度、表情偏好、口头禅、应答习惯）。数字分身就是靠这份素材
          检索你的风格。全库扫描约 1-2 分钟，期间可以做别的。
        </div>

        <div v-if="running" class="notice" style="margin-top:12px">
          <span class="spinner" style="margin-right:8px"></span>
          正在全库扫描收集语料（约 1-2 分钟）… 已等待 {{ elapsed }} 秒
        </div>

        <div v-else class="row" style="margin-top:12px">
          <button class="btn btn-primary" @click="generate" :disabled="busy">
            {{ state.corpus ? '重新生成' : '开始生成' }}
          </button>
          <label v-if="state.corpus" class="row tiny muted" style="gap:6px">
            <input type="checkbox" v-model="confirmRebuild" />
            确认覆盖现有语料包
          </label>
        </div>

        <div v-if="genErr" class="tiny" style="color:var(--danger);margin-top:10px">
          {{ genErr }}
        </div>
        <div v-if="genDone" class="notice" style="margin-top:12px">
          ✅ 生成完成：语料 {{ fmtNum(genDone.corpus) }} 条 /
          {{ genDone.sessions }} 个会话，平均 {{ genDone.avgLen }} 字。
          风格画像已更新，可在下方预览或下载。
        </div>
      </div>

      <div class="card" style="margin-top:16px" v-if="md">
        <div class="card-title">风格画像预览</div>
        <div class="card-sub">分身的「你怎么说话」说明书，纯本机统计生成。</div>
        <pre class="md-pre">{{ md }}</pre>
      </div>

      <div class="card" style="margin-top:16px" v-if="state.styleProfile">
        <div class="card-title">下载副本</div>
        <div class="card-sub">
          语料包本体在项目 persona/ 目录（分身直接读它，不用搬走）；
          这里提供副本下载，方便备份或带走。
        </div>
        <div class="row" style="margin-top:12px">
          <button class="btn" @click="download('style_profile.md')">
            下载风格画像（md）
          </button>
          <button class="btn" @click="download('corpus.jsonl')">
            下载语料包（jsonl）
          </button>
        </div>
      </div>

      <div class="card" style="margin-top:16px">
        <div class="card-title">说明</div>
        <ul class="tiny muted" style="padding-left:18px;line-height:1.9">
          <li>只收集<strong>你本人发出</strong>的消息；对方的内容不进语料</li>
          <li>纯本机处理，不上传任何数据；产物在 gitignore 的 persona/ 里</li>
          <li>聊天记录变动后重新生成，分身才能学到最新的说话方式</li>
          <li>控制台的「分身对话」用的就是这份语料做风格检索</li>
        </ul>
      </div>
    </div>`,
    data() {
      return {
        state: { exists: false, path: "", corpus: 0, sessions: 0,
                 styleProfile: false, mtime: "" },
        md: "", busy: false, running: false, elapsed: 0,
        confirmRebuild: false, genErr: "", genDone: null,
        _timer: null, _tick: null,
      };
    },
    computed: {
      fmtNum() { return M.fmtNum; },
    },
    methods: {
      async load() {
        try {
          const d = await M.api.get("/api/persona-corpus");
          this.state = d.state || this.state;
          if (this.state.styleProfile && !this.md) this.loadMd();
        } catch (e) { /* 忽略 */ }
      },
      async loadMd() {
        try {
          const d = await M.api.get("/api/persona-corpus/md");
          this.md = d.md || "";
        } catch (e) { /* 还没生成 */ }
      },
      async generate() {
        this.busy = true; this.genErr = ""; this.genDone = null;
        try {
          const d = await M.api.post("/api/persona-corpus",
                                     { rebuild: this.confirmRebuild });
          this.running = true;
          this.elapsed = 0;
          this._tick = setInterval(() => { this.elapsed += 1; }, 1000);
          this._timer = setInterval(() => this.poll(d.taskId), 2000);
          this.poll(d.taskId);
        } catch (e) {
          this.genErr = e.message || String(e);
        } finally { this.busy = false; }
      },
      async poll(tid) {
        try {
          const t = await M.api.get("/api/task", { id: tid });
          if (t.status === "done") {
            this.stopTimers();
            this.running = false;
            const r = (t.result || {}).result || {};
            this.genDone = {
              corpus: (r.corpus || {}).corpus || 0,
              sessions: (r.corpus || {}).sessions || 0,
              avgLen: (r.profile || {}).avgLen || "—",
            };
            this.confirmRebuild = false;
            this.md = "";
            await this.load();
            M.toast.ok("语料包生成完成");
            return;
          }
          if (t.status === "error") {
            this.stopTimers();
            this.running = false;
            this.genErr = t.error || "生成失败";
          }
        } catch (e) { /* 网络抖动：下一轮再试 */ }
      },
      stopTimers() {
        if (this._timer) { clearInterval(this._timer); this._timer = null; }
        if (this._tick) { clearInterval(this._tick); this._tick = null; }
      },
      download(name) {
        const a = document.createElement("a");
        a.href = "/api/persona-corpus/file?name=" + encodeURIComponent(name);
        a.download = name;
        document.body.appendChild(a);
        a.click();
        a.remove();
      },
    },
    async mounted() { await this.load(); },
    beforeUnmount() { this.stopTimers(); },
  };

  global.MosuViews = global.MosuViews || {};
  Object.assign(global.MosuViews, { Persona });
})(window);
