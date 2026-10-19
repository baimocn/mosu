/* ==========================================================================
   页面 · 大模型配置 / 设置 / 首启向导
   向导的目标：一台新电脑上，用户只需要「微信开着 + 点两下」就能用上墨溯。
   取密钥失败时不甩术语，给可执行的下一步；需要 AI 辅助时才要求填 URL / Key。
   ========================================================================== */
(function (global) {
  "use strict";
  const M = global.Mosu;

  /* ---------------------------------------------------------------- LLM 配置卡 */
  const LlmCard = {
    template: `
    <div class="card">
      <div class="card-title">
        大模型（可选）
        <div class="spacer"></div>
        <span class="badge" :class="cfg && cfg.hasKey ? 'badge-success' : ''">
          {{ cfg && cfg.hasKey ? '已配置' : '未配置' }}
        </span>
      </div>
      <div class="card-sub">
        墨溯的解密、统计、画像全部离线完成，不依赖大模型。
        只有「数字分身对话」和「取密钥失败时的 AI 辅助诊断」需要它。
        密钥由你自己填，墨溯不内置任何服务端地址。
      </div>
      <div class="field">
        <label class="field-label">接口地址（Base URL）</label>
        <input class="input mono" v-model="baseUrl" placeholder="https://api.openai.com/v1" />
        <div class="field-hint">任何 OpenAI 兼容端点都行（官方 / 中转 / 本地 ollama、vLLM）。</div>
      </div>
      <div class="field">
        <label class="field-label">API Key</label>
        <input class="input mono" type="password" v-model="apiKey"
               :placeholder="cfg && cfg.hasKey ? '已保存（留空表示不修改）' : 'sk-…'" />
      </div>
      <div class="field">
        <label class="field-label">模型名</label>
        <input class="input mono" v-model="model" placeholder="gpt-4o-mini" />
      </div>
      <div class="row">
        <button class="btn" @click="test" :disabled="testing">
          <span v-if="testing" class="spinner"></span>测试连接
        </button>
        <button class="btn btn-primary" @click="save" :disabled="saving">
          <span v-if="saving" class="spinner"></span>保存
        </button>
        <div class="spacer"></div>
        <span v-if="cfg && cfg.source==='env'" class="tiny muted">
          当前生效值来自环境变量，保存后仍需以配置为准时请取消环境变量
        </span>
      </div>
      <div v-if="testResult" class="check-row" style="margin-top:12px">
        <div class="check-icon">{{ testResult.ok ? '✅' : '⚠️' }}</div>
        <div style="min-width:0">
          <div class="check-name">
            {{ testResult.ok ? '连接成功' : '连接失败' }}
            <span v-if="testResult.ok" class="tiny muted">
              · {{ testResult.latencyMs }}ms · {{ testResult.model }}
            </span>
          </div>
          <div class="check-detail">
            {{ testResult.ok ? (testResult.reply || '') : (testResult.hint || testResult.error) }}
          </div>
        </div>
      </div>
    </div>`,
    props: { cfg: { default: null } },
    data() { return { baseUrl: "", apiKey: "", model: "", testing: false, saving: false, testResult: null }; },
    watch: {
      cfg: { immediate: true, handler(c) {
        if (!c) return;
        this.baseUrl = c.baseUrl || "";
        this.model = c.model || "";
      } },
    },
    methods: {
      async test() {
        this.testing = true; this.testResult = null;
        try {
          this.testResult = await M.api.post("/api/llm/test", {
            baseUrl: this.baseUrl, apiKey: this.apiKey, model: this.model });
        } catch (e) { this.testResult = { ok: false, error: e.message }; }
        finally { this.testing = false; }
      },
      async save() {
        this.saving = true;
        try {
          await M.api.post("/api/llm", {
            baseUrl: this.baseUrl, apiKey: this.apiKey, model: this.model });
          M.toast.ok("大模型配置已保存");
          this.$emit("saved");
        } catch (e) { M.toast.err("保存失败：" + e.message); }
        finally { this.saving = false; }
      },
    },
  };

  /* ---------------------------------------------------------------- 设置 */
  const Settings = {
    components: { LlmCard },
    template: `
    <div class="page">
      <div class="grid grid-2">
        <div class="card">
          <div class="card-title">数据目录与密钥</div>
          <dl class="kv" v-if="cfg">
            <dt>配置目录</dt><dd class="mono">{{ cfg.mosuHome }}</dd>
            <dt>当前账号</dt><dd>{{ cfg.current || '—' }}</dd>
            <dt>数据目录</dt><dd class="mono" style="word-break:break-all">{{ cfg.dbPath || '—' }}</dd>
            <dt>数据库密钥</dt>
            <dd>
              <span class="badge" :class="cfg.hasKey ? 'badge-success' : 'badge-warn'">
                {{ cfg.hasKey ? '已配置 ' + cfg.keyPreview : '未配置' }}
              </span>
            </dd>
            <dt>图片密钥</dt>
            <dd>
              <span class="badge" :class="cfg.hasImgKey ? 'badge-success' : ''">
                {{ cfg.hasImgKey ? '已配置' : '未配置（V2 加密图片会显示不了）' }}
              </span>
            </dd>
          </dl>
          <div class="divider"></div>
          <div class="row">
            <button class="btn btn-primary" @click="$emit('wizard')">重新自动获取密钥</button>
            <button class="btn" @click="testKey">校验当前密钥</button>
          </div>
          <div v-if="keyTest" class="check-row" style="margin-top:12px">
            <div class="check-icon">{{ keyTest.hmacOk ? '✅' : '❌' }}</div>
            <div>
              <div class="check-name">
                {{ keyTest.hmacOk ? '密钥有效，可以解密' : '密钥不匹配' }}
              </div>
              <div class="check-detail mono" style="word-break:break-all">{{ keyTest.db }}</div>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-title">外观</div>
          <div class="field">
            <label class="field-label">主题</label>
            <div class="row">
              <button class="chip" :class="{active: theme==='light'}" @click="setTheme('light')">浅色</button>
              <button class="chip" :class="{active: theme==='dark'}" @click="setTheme('dark')">深色</button>
              <button class="chip" :class="{active: theme==='auto'}" @click="setTheme('auto')">跟随系统</button>
            </div>
          </div>
          <div class="divider"></div>
          <div class="card-title">运行环境</div>
          <dl class="kv">
            <dt>服务地址</dt><dd class="mono">127.0.0.1:{{ port }}</dd>
            <dt>环境变量覆盖</dt>
            <dd>
              <span class="tiny muted" v-if="cfg">
                MOSU_DB_PATH {{ cfg.envOverrides.MOSU_DB_PATH ? '✓' : '—' }} ·
                MOSU_KEY_HEX {{ cfg.envOverrides.MOSU_KEY_HEX ? '✓' : '—' }} ·
                MOSU_IMG_KEY_HEX {{ cfg.envOverrides.MOSU_IMG_KEY_HEX ? '✓' : '—' }}
              </span>
            </dd>
            <dt>微信通道</dt>
            <dd>
              <span class="badge" :class="wxOk ? 'badge-success' : ''">
                {{ wxOk ? '已连接' : '未连接' }}
              </span>
            </dd>
          </dl>
        </div>
      </div>

      <div style="margin-top:16px">
        <llm-card :cfg="llm" @saved="refresh" />
      </div>

      <div class="card" style="margin-top:16px">
        <div class="card-title">关于与安全</div>
        <ul class="tiny muted" style="padding-left:18px;line-height:1.9">
          <li>全部处理在本机完成，聊天数据不上传任何服务器</li>
          <li>取密钥只用 <span class="mono">ReadProcessMemory</span> 只读读取微信内存，
              不注入、不 hook、不改微信任何文件</li>
          <li>密钥与大模型 Key 都保存在本机 {{ cfg ? cfg.mosuHome : '~/.mosu' }}，
              请勿提交到仓库或网盘</li>
          <li>仅用于备份与分析你自己的数据</li>
        </ul>
      </div>
    </div>`,
    data() { return { keyTest: null, port: location.port }; },
    computed: {
      cfg() { return M.store.config; },
      llm() { return M.store.llm; },
      theme() { return M.theme.value; },
      wxOk() { return !!(M.store.status && M.store.status.wechat && M.store.status.wechat.logged_in); },
    },
    methods: {
      setTheme(t) { M.setTheme(t); },
      async testKey() {
        try { this.keyTest = await M.api.post("/api/key", { action: "test" }); }
        catch (e) { M.toast.err("校验失败：" + e.message); }
      },
      async refresh() { await M.refreshConfig(); },
    },
    mounted() { if (!M.store.config) M.refreshConfig(); },
  };

  /* ---------------------------------------------------------------- 首启向导 */
  const Wizard = {
    components: { LlmCard },
    template: `
    <div class="wizard">
      <div class="wizard-card">
        <div class="wizard-head">
          <div class="wizard-title">{{ title }}</div>
          <div class="wizard-sub">{{ sub }}</div>
        </div>
        <div class="wizard-steps">
          <template v-for="(s,i) in steps" :key="s">
            <div class="wizard-step" :class="stepClass(i)">
              <span class="wizard-dot">{{ i < step ? '✓' : i+1 }}</span>{{ s }}
            </div>
            <div v-if="i < steps.length-1" class="wizard-step-line"></div>
          </template>
        </div>

        <div class="wizard-body">
          <!-- 1 欢迎 -->
          <div v-if="step===0">
            <div class="grid grid-2" style="gap:12px">
              <div class="check-row"><div class="check-icon">🔒</div>
                <div><div class="check-name">全程本机</div>
                  <div class="check-detail">解密、统计、画像都在你电脑上跑，不上传</div></div></div>
              <div class="check-row"><div class="check-icon">🛡</div>
                <div><div class="check-name">只读取密钥</div>
                  <div class="check-detail">不注入、不 hook、不改微信文件</div></div></div>
              <div class="check-row"><div class="check-icon">🧠</div>
                <div><div class="check-name">不强制联网</div>
                  <div class="check-detail">核心功能不需要大模型，需要时你自己填</div></div></div>
              <div class="check-row"><div class="check-icon">📦</div>
                <div><div class="check-name">导出属于你</div>
                  <div class="check-detail">HTML 存档 / CSV / JSON / TXT 随你选</div></div></div>
            </div>
          </div>

          <!-- 2 环境检测 -->
          <div v-else-if="step===1">
            <div v-if="!probe" class="skeleton" style="height:120px"></div>
            <template v-else>
              <div class="check-row">
                <div class="check-icon">{{ probe.processOk ? '✅' : '⚠️' }}</div>
                <div style="min-width:0;flex:1">
                  <div class="check-name">微信进程</div>
                  <div class="check-detail">
                    {{ probe.processOk
                      ? '检测到 ' + probe.processes.length + ' 个微信进程，最大 ' +
                        probe.processes[0].memMB + ' MB'
                      : '没有检测到运行中的微信' }}
                  </div>
                </div>
                <div class="check-status">
                  <span class="badge" :class="probe.processOk ? 'badge-success' : 'badge-warn'">
                    {{ probe.processOk ? '就绪' : '需要处理' }}
                  </span>
                </div>
              </div>
              <div class="check-row">
                <div class="check-icon">{{ probe.dbOk ? '✅' : '⚠️' }}</div>
                <div style="min-width:0;flex:1">
                  <div class="check-name">微信数据目录</div>
                  <div class="check-detail mono" style="word-break:break-all">
                    {{ probe.db || '未找到' }}
                  </div>
                </div>
              </div>
              <div class="check-row">
                <div class="check-icon">{{ probe.maskOk ? '✅' : '⚠️' }}</div>
                <div style="min-width:0;flex:1">
                  <div class="check-name">版本适配</div>
                  <div class="check-detail">
                    {{ probe.maskOk
                      ? '已提取到 ' + probe.maskCount + ' 个版本掩码'
                      : '未能提取版本掩码（可能是很新/很旧的微信版本）' }}
                  </div>
                </div>
              </div>
              <div v-if="!probe.processOk" class="check-row" style="border-color:var(--warn)">
                <div class="check-icon">💡</div>
                <div>
                  <div class="check-name">请先登录微信</div>
                  <div class="check-detail">
                    墨溯从运行中的微信进程里读取密钥，电脑版微信必须处于登录状态。
                    登录后点「重新检测」。
                  </div>
                </div>
              </div>
            </template>
          </div>

          <!-- 3 取密钥 -->
          <div v-else-if="step===2">
            <div v-if="!running && !result" class="scan-stage">
              <div style="font-size:40px">🔑</div>
              <div class="scan-msg">
                将只读扫描微信进程内存提取密钥，约 30 秒。不会写入微信、不会改动任何文件。
              </div>
              <button class="btn btn-primary btn-lg" @click="findKey">开始自动获取</button>
            </div>
            <div v-else-if="running" class="scan-stage">
              <div class="progress"><div class="progress-bar indeterminate"></div></div>
              <div class="scan-msg">{{ stageMsg }}</div>
              <div class="scan-meta">{{ stage }} · {{ stageMeta }}</div>
              <button class="btn btn-sm" @click="cancel">取消</button>
            </div>
            <div v-else-if="result && result.ok" class="scan-stage">
              <div style="font-size:44px">🎉</div>
              <div class="scan-msg">
                密钥已获取并保存
                <span class="mono">{{ masked(result.keyHex) }}</span>
              </div>
              <div class="scan-meta">
                候选 {{ result.candidates }} 个 · 校验 {{ result.tried }} 次 ·
                耗时 {{ result.elapsed }}s
              </div>
            </div>
            <div v-else-if="result" class="col">
              <div class="check-row" style="border-color:var(--danger)">
                <div class="check-icon">⚠️</div>
                <div>
                  <div class="check-name">自动获取失败（{{ result.stage }}）</div>
                  <div class="check-detail">{{ result.error }}</div>
                  <div class="check-detail" style="margin-top:4px">{{ result.hint }}</div>
                </div>
              </div>
              <div class="divider"></div>
              <div class="field">
                <label class="field-label">手动填入密钥（64 位十六进制）</label>
                <input class="input mono" v-model="manualKey" placeholder="用其他工具取到后粘贴到这里" />
              </div>
              <div class="row">
                <button class="btn btn-primary" @click="saveManual" :disabled="!manualKey">保存并继续</button>
                <button class="btn" @click="retry">重试自动获取</button>
                <div class="spacer"></div>
                <button class="btn btn-ghost" @click="aiHelp = !aiHelp">
                  {{ aiHelp ? '收起 AI 辅助' : 'AI 辅助诊断' }}
                </button>
              </div>
              <llm-card v-if="aiHelp" :cfg="llm" @saved="onLlmSaved" style="margin-top:12px" />
              <div v-if="aiHelp && llm && llm.hasKey" class="card" style="margin-top:12px">
                <div class="card-title">把诊断信息交给大模型</div>
                <div class="card-sub">
                  复制下面这段（不含密钥等敏感值的诊断摘要）发给你的大模型，让它判断原因。
                </div>
                <textarea class="input mono" rows="8" readonly :value="diagText"
                          @focus="$event.target.select()"></textarea>
              </div>
            </div>
          </div>

          <!-- 4 完成 -->
          <div v-else-if="step===3" class="scan-stage">
            <div style="font-size:44px">✨</div>
            <div class="scan-msg">一切就绪，开始探索你的微信数据吧</div>
            <div class="scan-meta">
              {{ sessionCount }} 个会话 · {{ msgCount }} 条消息
            </div>
          </div>
        </div>

        <div class="wizard-foot">
          <button class="btn btn-ghost" v-if="step>0 && !running" @click="step--">上一步</button>
          <div class="spacer"></div>
          <button class="btn" v-if="step===1" @click="probeNow">重新检测</button>
          <button class="btn btn-primary" v-if="step===0" @click="next">开始</button>
          <button class="btn btn-primary" v-if="step===1" @click="next"
                  :disabled="!(probe && (probe.processOk || probe.dbOk))">下一步</button>
          <button class="btn btn-primary" v-if="step===3" @click="$emit('done')">进入墨溯</button>
        </div>
      </div>
    </div>`,
    data() {
      return {
        step: 0, steps: ["欢迎", "环境检测", "获取密钥", "完成"],
        probe: null, running: false, result: null, es: null,
        stage: "", stageMsg: "", stageMeta: "",
        manualKey: "", aiHelp: false, sessionCount: 0, msgCount: 0,
      };
    },
    computed: {
      llm() { return M.store.llm; },
      title() {
        return ["欢迎使用墨溯", "检查运行环境", "获取数据库密钥", "准备就绪"][this.step];
      },
      sub() {
        return [
          "本地微信数据的解密、浏览与导出工具",
          "确认微信在跑、数据目录找得到、版本可适配",
          "从运行中的微信进程里只读提取密钥",
          "可以开始浏览了",
        ][this.step];
      },
      diagText() {
        const p = this.probe || {};
        const r = this.result || {};
        return [
          "墨溯密钥获取诊断（不含任何密钥/隐私数据）",
          "--------------------------------",
          "微信进程: " + (p.processOk ? "在跑，共 " + (p.processes || []).length + " 个" : "未检测到"),
          "数据目录: " + (p.db || "未找到"),
          "版本掩码: " + (p.maskOk ? p.maskCount + " 个" : "未提取到"),
          "dll: " + (p.dll || "未找到"),
          "失败阶段: " + (r.stage || "-"),
          "错误: " + (r.error || "-"),
          "候选数: " + (r.candidates || 0) + " / 校验次数: " + (r.tried || 0),
          "",
          "请分析可能原因并给出可执行的下一步建议。",
        ].join("\n");
      },
    },
    methods: {
      masked(k) { return k ? k.slice(0, 8) + "…" + k.slice(-8) : "—"; },
      stepClass(i) {
        if (i < this.step) return "done";
        if (i === this.step) return "current";
        return "";
      },
      async next() {
        if (this.step === 1) { this.step = 2; return; }
        this.step++;
        if (this.step === 1) await this.probeNow();
        if (this.step === 3) await this.summary();
      },
      async probeNow() {
        this.probe = null;
        try { this.probe = await M.api.get("/api/key/selftest"); }
        catch (e) { M.toast.err("检测失败：" + e.message); this.probe = { ok: false }; }
      },
      findKey() {
        this.running = true; this.result = null;
        this.stage = "process"; this.stageMsg = "正在检测微信进程…"; this.stageMeta = "";
        const label = {
          process: "检测微信进程", mask: "提取版本适配信息",
          scan: "扫描进程内存", deref: "解析候选密钥",
          verify: "校验候选（PBKDF2 + HMAC）", save: "写入密钥库",
        };
        this.es = M.api.stream("/api/key/find", {
          progress: (d) => {
            this.stage = d.stage;
            this.stageMsg = d.message || (label[d.stage] || d.stage);
            this.stageMeta = d.total ? `${d.current}/${d.total}` : "";
          },
          done: (d) => {
            this.running = false; this.result = d;
            if (d.ok) {
              M.toast.ok("密钥已获取并保存");
              M.store.sessionsLoaded = false;
              setTimeout(() => { this.step = 3; this.summary(); }, 600);
            }
            this.closeStream();
          },
          error: (d) => {
            this.running = false; this.result = d;
            this.closeStream();
          },
          end: () => {
            if (this.running) {   // 流断了但没结果：当作失败处理，别卡在进度条
              this.running = false;
              this.result = { ok: false, stage: "stream",
                error: "扫描连接中断", hint: "请重试；仍失败可用手动填入密钥。" };
            }
          },
        });
      },
      closeStream() { if (this.es) { try { this.es.close(); } catch (e) {} this.es = null; } },
      cancel() { this.closeStream(); this.running = false; M.toast("已取消"); },
      retry() { this.result = null; this.findKey(); },
      async saveManual() {
        const k = this.manualKey.trim();
        if (!/^[0-9a-fA-F]{64}$/.test(k)) { M.toast.err("必须是 64 位十六进制字符串"); return; }
        try {
          await M.api.post("/api/key", { action: "set", keyHex: k });
          M.toast.ok("密钥已保存");
          M.store.sessionsLoaded = false;
          this.step = 3; await this.summary();
        } catch (e) { M.toast.err("保存失败：" + e.message); }
      },
      async onLlmSaved() { await M.refreshConfig(); },
      async summary() {
        try {
          M.refreshStatus();
          const d = await M.api.get("/api/sessions", { limit: 500 });
          this.sessionCount = d.total || (d.sessions || []).length;
          this.msgCount = (d.sessions || []).reduce((a, s) => a + (s.messageCount || 0), 0);
        } catch (e) { /* 忽略 */ }
      },
    },
    async mounted() {
      await M.refreshConfig();
      // 已有可用密钥时不必再走一遍：直接跳到完成
      try {
        const st = await M.api.get("/api/status");
        if (st && st.data && st.data.ready) {
          this.step = 3; this.summary();
        }
      } catch (e) { /* 忽略 */ }
    },
    beforeUnmount() { this.closeStream(); },
  };

  global.MosuViews = global.MosuViews || {};
  Object.assign(global.MosuViews, { LlmCard, Settings, Wizard });
})(window);
