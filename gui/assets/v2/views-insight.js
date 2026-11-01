/* ==========================================================================
   墨溯 v2 · 洞察工作台（Agentic）
   --------------------------------------------------------------------------
   2026-11-03 重构第二版。上一版是「固定管道」：后端预先塞 7 条事实 + 8 条
   语料样本，模型一次调用就结束。用户反馈「ai 功能好差劲」——根因不是模型，
   是**模型被绑住了手**：问「我和某人上个月聊了什么」，它压根看不到那份记录。

   现在改成 Agentic：模型自己调工具查数据，工具调用轨迹实时可见。
   三块核心：
     1. 轨迹（steps）  —— 它查了什么、查到了什么，一步步摊开给用户看
     2. 证据（evidence）—— 每条带时间+说话人+原文，可点开核对
     3. 多轮追问      —— 可以接着问「那上个月呢」，上下文带得上

   与竞品 CipherTalk 的差异（有意为之）：
     - 竞品靠 embedding 做语义检索，墨溯零外部依赖 → 关键词 + 精确时间 + 会话定向
     - 竞品给模型 query_sql 兜底，墨溯**不给** → 数字只能来自工具，不会被编出来
   ========================================================================== */
(function (global) {
  "use strict";
  const M = global.Mosu;
  const { ref, computed, onMounted, nextTick, watch } = Vue;

  const Insight = {
    props: { route: { type: String, default: "insight" } },
    // 子组件必须**在本视图里**注册，不能指望父组件（App）注册——
    // Vue 的局部组件注册是**向下**可见的：App 注册了，App 的子组件里用不了。
    // 踩过这个坑：只在上层注册 → <branch-tree> 成了未知标签，
    // **Vue 静默忽略不报错**，页面看着正常但分支树永远不出现。
    components: { "branch-tree": global.MosuV2.BranchTree },
    template: `
    <div class="v2-page v2-scroll v2-ia">
      <div class="v2-page-head">
        <div>
          <h1 class="v2-page-title">洞察</h1>
          <p class="v2-page-desc">
            大模型会自己查你的
            <strong>{{ M.fmtNum(libTotal) }}</strong> 条聊天记录来回答，
            查了什么、依据什么，都摊开给你看。
          </p>
        </div>
        <div v-if="model" class="v2-ia-model">
          <span class="v2-ia-model-dot"></span>{{ model }}
        </div>
        <button class="v2-ia-hist-btn" @click="toggleHistory">
          <span class="v2-ia-hist-caret" :class="{open: histOpen}">›</span>
          历史分析<span v-if="history.length" class="v2-ia-hist-n">{{ history.length }}</span>
        </button>
      </div>

      <!-- ============ 历史分析（落盘在 ~/.mosu/runs/，重启不丢） ============ -->
      <div v-if="histOpen" class="v2-ia-hist" v-cloak>
        <div class="v2-ia-hist-head">
          <span class="v2-ia-hist-title">历史分析</span>
          <span class="v2-ia-hist-desc">每轮完整落盘，关页面、重启都还在</span>
          <button class="v2-ia-hist-refresh" @click="loadHistory">刷新</button>
        </div>
        <div v-if="histLoading" class="v2-note">正在读历史…</div>
        <div v-else-if="!history.length" class="v2-note">还没有历史记录。问第一个问题吧。</div>
        <div v-else class="v2-ia-hist-list">
          <div v-for="h in history" :key="h.id" class="v2-ia-hist-item"
               :class="{open: histOpenId === h.id}" @click="openHistory(h)">
            <div class="v2-ia-hist-q">{{ h.question || '（无标题）' }}</div>
            <div class="v2-ia-hist-meta">
              <span>{{ fmtTime(h.createdAt) }}</span>
              <span v-if="h.steps">{{ h.steps }} 步</span>
              <span v-if="h.branches">{{ h.branches }} 分支</span>
              <span v-if="h.cost">{{ h.cost }}s</span>
              <button class="v2-ia-hist-del" @click.stop="delHistory(h)">删</button>
            </div>
            <div v-if="histOpenId === h.id && histDetail" class="v2-ia-hist-detail">
              <article class="v2-ia-answer small" v-html="rendered(histDetail)"></article>
            </div>
          </div>
        </div>
      </div>

      <!-- ============ 提问区 ============ -->
      <div class="v2-ia-ask-wrap">
        <div class="v2-ia-ask">
          <textarea
            v-model="question"
            class="v2-ia-input"
            :placeholder="ph"
            rows="1"
            @keydown.enter.exact.prevent="submit"
            @input="autoGrow"></textarea>
          <button class="v2-btn v2-ia-go" :disabled="!question.trim() || busy" @click="submit">
            <span v-if="busy" class="v2-spin"></span>
            <span v-else>问</span>
          </button>
        </div>
        <div v-if="!turns.length && presets.length" class="v2-ia-presets">
          <div class="v2-ia-presets-label">试试这些问题</div>
          <div class="v2-ia-preset-row">
            <button v-for="p in presets" :key="p.id"
                    class="v2-ia-preset" @click="usePreset(p)">
              <div class="v2-ia-preset-q">{{ p.q }}</div>
              <div class="v2-ia-preset-h">{{ p.hint }}</div>
            </button>
          </div>
        </div>

        <!-- 库里有：首屏给出可提问的素材范围 -->
        <div v-if="!turns.length && libTotal" class="v2-ia-scope">
          <div class="v2-ia-sec-title">库里有这些，可以随便问</div>
          <div class="v2-ia-scope-row">
            <div class="v2-ia-scope-item">
              <div class="v2-ia-scope-val">{{ M.fmtNum(libTotal) }}</div>
              <div class="v2-ia-scope-lab">条消息</div>
            </div>
            <div class="v2-ia-scope-item">
              <div class="v2-ia-scope-val">{{ M.fmtNum(libSessions) }}</div>
              <div class="v2-ia-scope-lab">个会话</div>
            </div>
            <div class="v2-ia-scope-item">
              <div class="v2-ia-scope-val">{{ M.fmtNum(libContacts) }}</div>
              <div class="v2-ia-scope-lab">个联系人</div>
            </div>
            <div class="v2-ia-scope-item">
              <div class="v2-ia-scope-val">{{ libSpan }}</div>
              <div class="v2-ia-scope-lab">时间跨度</div>
            </div>
          </div>
          <div v-if="topNames.length" class="v2-ia-scope-top">
            <span class="v2-ia-scope-top-lab">聊得最多的</span>
            <span v-for="(t, i) in topNames" :key="i" class="v2-ia-chip">
              {{ t.name }}<span class="v2-ia-chip-n">{{ M.fmtNum(t.total) }}</span>
            </span>
          </div>
        </div>
      </div>

      <!-- ============ 对话轮次 ============ -->
      <div v-for="(t, ti) in turns" :key="ti" class="v2-ia-turn">
        <!-- 问题 -->
        <div class="v2-ia-q-echo">
          <span class="v2-ia-q-mark">问</span>{{ t.question }}
        </div>

        <!-- 运行中：工具轨迹 -->
        <div v-if="t.pending" class="v2-ia-running">
          <div class="v2-ia-status-row">
            <span class="v2-spin"></span>
            <span>{{ t.stage || "正在思考…" }}</span>
          </div>
          <div class="v2-ia-progress"><div class="v2-ia-progress-bar"></div></div>
          <div v-if="t.steps && t.steps.length" class="v2-ia-steps">
            <div v-for="s in t.steps" :key="s.index" class="v2-ia-step">
              <span class="v2-ia-step-no">{{ s.index }}</span>
              <span class="v2-ia-step-label">{{ s.label }}</span>
              <span class="v2-ia-step-sum">{{ s.summary }}</span>
            </div>
          </div>
          <!-- 派了子代理：并行时告诉用户有几路在查 -->
          <div v-if="(t.branches || []).length" class="v2-ia-branch-live">
            已派出 {{ t.branches.length }} 个子代理，正在并行查询…
          </div>
        </div>

        <!-- 失败 -->
        <div v-else-if="t.error" class="v2-note danger">{{ t.error }}</div>

        <!-- 回答 -->
        <template v-else>
          <!-- 阶段契约（对标 Lancer PROCESS.json）：这条回答过了哪些环节、哪些没过 -->
          <div v-if="t.contract && t.contract.stages && t.contract.stages.length"
               class="v2-ia-contract"
               :class="{blocked: !t.contract.okToProceed}">
            <div class="v2-ia-steps-head">
              <span class="v2-ia-steps-title">验收环节</span>
              <span class="v2-ia-steps-count">
                {{ t.contract.okToProceed ? "全部通过" : "有环节未通过" }}
              </span>
            </div>
            <div class="v2-ia-contract-stages">
              <div v-for="s in t.contract.stages" :key="s.id"
                   class="v2-ia-cstage"
                   :class="{bad: !s.status.okToProceed, skip: s.status.code === 'SKIPPED'}"
                   :data-stage-id="s.id"
                   :data-status-code="s.status.code"
                   :data-ok-to-proceed="String(s.status.okToProceed)">
                <span class="v2-ia-cstage-dot"></span>
                <span class="v2-ia-cstage-label">{{ s.label }}</span>
                <span class="v2-ia-cstage-code">{{ s.status.label }}</span>
                <span class="v2-ia-cstage-sum">{{ s.summary }}</span>
              </div>
            </div>
            <div v-if="!t.contract.okToProceed" class="v2-ia-contract-warn">
              <div v-for="s in t.contract.stages" :key="'w' + s.id"
                   v-show="!s.status.okToProceed && s.status.hint"
                   class="v2-ia-contract-warn-row">
                <b>{{ s.label }}</b>：{{ s.status.hint }}
              </div>
            </div>
            <div v-if="(t.contract.forbidden || []).some(f => f.violated)"
                 class="v2-ia-contract-forbid">
              <div class="v2-ia-contract-forbid-title">违反的禁止项</div>
              <div v-for="f in t.contract.forbidden" :key="f.id"
                   v-show="f.violated" class="v2-ia-contract-forbid-row">
                <b>{{ f.text }}</b>
                <span v-if="f.detail">—— {{ f.detail }}</span>
              </div>
            </div>
          </div>

          <!-- 交叉核验：答案由谁写的、跟查数据的是不是同一个模型 -->
          <div v-if="t.contract && t.contract.crossModel
                    && t.contract.crossModel.collectModel"
               class="v2-ia-crossmodel"
               :class="{on: t.contract.crossModel.predicateSatisfied}"
               :data-predicate="t.contract.crossModel.predicate"
               :data-predicate-satisfied="String(t.contract.crossModel.predicateSatisfied)">
            <div class="v2-ia-steps-head">
              <span class="v2-ia-steps-title">交叉核验</span>
              <span class="v2-ia-steps-count">
                {{ t.contract.crossModel.predicateSatisfied ? '已生效' : '未生效' }}
              </span>
            </div>
            <div class="v2-ia-crossmodel-body">
              <span class="v2-ia-crossmodel-item">
                取证：<b>{{ t.contract.crossModel.collectModel }}</b>
              </span>
              <span class="v2-ia-crossmodel-item">
                成文：<b>{{ t.contract.crossModel.writeModel || '（未配置）' }}</b>
              </span>
            </div>
            <div v-if="t.contract.crossModel.composeError"
                 class="v2-ia-crossmodel-note">
              {{ t.contract.crossModel.composeError }}
            </div>
            <div v-else-if="!t.contract.crossModel.distinct"
                     class="v2-ia-crossmodel-note">
              取证与成文是同一个模型 —— 相当于自己查自己确认，不构成交叉核验。
            </div>
          </div>

          <!-- 工具轨迹（完成后仍展示，这是可信度的来源） -->
          <div v-if="t.steps && t.steps.length" class="v2-ia-steps done">
            <div class="v2-ia-steps-head">
              <span class="v2-ia-steps-title">它是这样查的</span>
              <span class="v2-ia-steps-count">{{ t.steps.length }} 步 · {{ t.cost || 0 }} 秒</span>
            </div>
            <div v-for="s in t.steps" :key="s.index" class="v2-ia-step">
              <span class="v2-ia-step-no">{{ s.index }}</span>
              <span class="v2-ia-step-label">{{ s.label }}</span>
              <span class="v2-ia-step-sum">{{ s.summary }}</span>
            </div>
          </div>

          <!-- 分支树（多代理）：泳道分列 + 语义锚点光线 -->
          <div v-if="t.branches && t.branches.length" class="v2-ia-branches">
            <div class="v2-ia-steps-head">
              <span class="v2-ia-steps-title">它派了 {{ t.branches.length }} 个子代理去查</span>
              <span class="v2-ia-steps-count">并行执行</span>
            </div>
            <branch-tree :branches="t.branches"></branch-tree>
          </div>

          <article class="v2-ia-answer" v-html="rendered(t)"></article>

          <!-- 证据出处 -->
          <div v-if="(t.evidence || []).length" class="v2-ia-cites">
            <!-- 原话：能点开看全文 -->
            <template v-if="quotes(t, ti).length">
              <div class="v2-ia-sec-title">它读了这些原话（点开看全文）</div>
              <div class="v2-ia-ev-row">
                <button v-for="q in quotes(t, ti)" :key="'q' + q._i"
                        class="v2-ia-ev" :class="{open: evOpen(ti, q._i)}"
                        @click="toggleEv(ti, q._i)">
                  <span class="v2-ia-ev-time">{{ q.time }}</span>
                  <span class="v2-ia-ev-sender">{{ q.sender }}</span>
                  <!-- 折叠显示摘要，展开显示全文（两者不同时出现，避免同一句显示两遍） -->
                  <span v-if="!(evOpen(ti, q._i) && q.full)" class="v2-ia-ev-text">{{ q.text }}</span>
                  <span v-if="evOpen(ti, q._i) && q.full" class="v2-ia-ev-full">{{ q.full }}</span>
                </button>
              </div>
            </template>
            <!-- 统计口径：数字来自 SQL，不是原话 -->
            <template v-if="stats(t).length">
              <div class="v2-ia-sec-title">它算出的数字（数据库直接算的）</div>
              <div class="v2-ia-ev-row">
                <div v-for="(e, ei) in stats(t)" :key="'s' + ei" class="v2-ia-ev v2-ia-ev-stat">
                  <span class="v2-ia-ev-time">{{ e.time }}</span>
                  <span class="v2-ia-ev-sender">{{ e.sender }}</span>
                  <span class="v2-ia-ev-text">{{ e.text }}</span>
                </div>
              </div>
            </template>
            <button v-if="(t.evidence || []).length > 6"
                    class="v2-ia-ev-more"
                    @click="evShown[ti] = (evShown[ti] || 6) >= (t.evidence || []).length ? 6 : (t.evidence || []).length">
              {{ (evShown[ti] || 6) >= (t.evidence || []).length ? '收起' : '展开全部 ' + (t.evidence || []).length + ' 条' }}
            </button>
          </div>

          <!-- 追问提示 -->
          <div class="v2-ia-follow">
            <span class="v2-ia-follow-tip">接着问</span>
            <button v-for="f in followUps(t)" :key="f"
                    class="v2-ia-follow-btn" @click="askFollow(f)">{{ f }}</button>
          </div>
        </template>
      </div>

      <!-- ============ 原始统计（证据的另一面） ============ -->
      <div class="v2-ia-raw">
        <button class="v2-ia-ev-toggle" @click="rawOpen = !rawOpen">
          <span class="v2-ia-ev-caret" :class="{open: rawOpen}">›</span>
          原始统计图表
          <span class="v2-ia-ev-count">{{ rawLoading ? '加载中…' : '不依赖模型，直接从数据库算' }}</span>
        </button>
        <div v-if="rawOpen" class="v2-ia-ev-body" v-cloak>
          <template v-if="rawData">
            <!-- 热力日历 -->
            <div class="v2-ia-sec-title">活跃日历</div>
            <p class="v2-ia-sec-desc">每格一天，颜色深浅代表消息量。</p>
            <div class="v2-heat-months">
              <span v-for="m in heatMonths" :key="m" class="v2-heat-month">{{ m }}</span>
            </div>
            <div class="v2-heat">
              <div v-for="(c,i) in calendar" :key="i"
                   class="v2-heat-cell"
                   :style="c ? {background: heatColor(c.level)} : {background:'transparent'}"
                   :title="c ? (c.date + ' · ' + c.n + ' 条') : ''"></div>
            </div>

            <!-- 时段 -->
            <div class="v2-ia-sec-title" style="margin-top:22px">时段分布</div>
            <p class="v2-ia-sec-desc">一天里哪个小时最活跃。</p>
            <div class="v2-bar-list">
              <div v-for="r in hourlyRows" :key="r.h" class="v2-bar-row">
                <span class="v2-bar-label">{{ r.h }}时</span>
                <div class="v2-bar-track">
                  <div class="v2-bar-fill" :style="{width: r.pct + '%'}"></div>
                </div>
                <span class="v2-bar-val">{{ M.fmtNum(r.n) }}</span>
              </div>
            </div>

            <!-- 话题 -->
            <div class="v2-ia-sec-title" style="margin-top:22px">话题分布</div>
            <p class="v2-ia-sec-desc">按你发出的内容归纳。</p>
            <div class="v2-bar-list">
              <div v-for="t in topics" :key="t.name" class="v2-bar-row">
                <span class="v2-bar-label wide">{{ t.name }}</span>
                <div class="v2-bar-track">
                  <div class="v2-bar-fill" :style="{width: t.pct + '%'}"></div>
                </div>
                <span class="v2-bar-val">{{ M.fmtNum(t.count) }}</span>
              </div>
            </div>
          </template>
          <div v-else class="v2-note">正在加载统计数据…</div>
        </div>
      </div>
    </div>
    `,

    setup(props) {
      const question = ref("");
      const presets = ref([]);
      // 对话轮次：[{question, answer, steps, evidence, cost, pending, error}]
      const turns = ref([]);
      const busy = ref(false);
      const error = ref("");
      const model = ref("");
      const libTotal = ref(0);
      const libSessions = ref(0);
      const libContacts = ref(0);
      const libSpan = ref("");
      const topNames = ref([]);
      const evShown = ref({});     // 每轮默认显示几条证据
      const evOpenMap = ref({});   // 哪几条被展开了（ti:ei → true）

      // 历史分析（落盘 ~/.mosu/runs/）
      const histOpen = ref(false);
      const histLoading = ref(false);
      const history = ref([]);
      const histOpenId = ref("");
      const histDetail = ref(null);

      // 原始统计
      const rawOpen = ref(false);
      const rawLoading = ref(false);
      const rawData = ref(null);
      const stageText = ref("正在思考…");

      const ph = computed(() => {
        const n = libTotal.value;
        if (!n) return "问我任何关于你聊天记录的事…";
        return `问我任何关于这 ${M.fmtNum(n)}条聊天的事，比如：我什么时候最话多？`;
      });

      let pollTimer = null;

      onMounted(async () => {
        if (props.route === "insight.raw") rawOpen.value = true;
        try {
          const p = await M.api.get("/api/insight/presets");
          presets.value = (p && p.presets) || [];
        } catch (e) {
          presets.value = [];
        }
        try {
          const s = await M.api.get("/api/stats/global");
          libTotal.value = (s && s.totalMessages) || 0;
          libSessions.value = (s && s.totalSessions) || 0;
          libContacts.value = (s && s.totalContacts) || 0;
          const tr = (s && s.timeRange) || {};
          if (tr.firstText && tr.lastText) {
            libSpan.value = tr.firstText.slice(0, 7) + " → " + tr.lastText.slice(0, 7);
          }
        } catch (e) {
          libTotal.value = 0;
        }
        try {
          const c = await M.api.get("/api/stats/contacts");
          const list = (c && (c.contacts || c.items)) || [];
          topNames.value = list.slice(0, 6).map((x) => ({
            name: x.displayName || x.name || x.wxid || "—",
            total: x.messageCount || x.count || x.total || 0,
          }));
        } catch (e) {
          topNames.value = [];
        }
      });

      // ---------------------------------------------------------------- 提问
      async function submit() {
        const q = question.value.trim();
        if (!q || busy.value) return;
        busy.value = true;
        error.value = "";
        question.value = "";
        const turn = {
          question: q,
          answer: "",
          steps: [],
          evidence: [],
          branches: [],
          // 阶段契约：跑完才有值，先占位（Vue 响应式要求字段预先存在）
          contract: {},
          cost: 0,
          pending: true,
          error: "",
          stage: "正在思考…",
        };
        turns.value.push(turn);
        const ti = turns.value.length - 1;
        try {
          // 带上前面的对话，让模型能接住追问
          const history = turns.value.slice(0, ti).flatMap((t) => [
            { role: "user", content: t.question },
            { role: "assistant", content: t.answer || "" },
          ]);
          const r = await M.api.post("/api/insight/ask", { question: q, history });
          await streamTask(r.taskId, ti);
        } catch (e) {
          turn.pending = false;
          turn.error = (e && e.message) || String(e);
          busy.value = false;
        }
      }

      function pollTask(tid, ti) {
        return new Promise((resolve, reject) => {
          const tick = async () => {
            try {
              const t = await M.api.get("/api/task", { id: tid });
              if (!t) throw new Error("任务丢失");
              const turn = turns.value[ti];
              if (!turn) return;
              // 实时轨迹：这是 Agentic 最重要的体验差异
              const live = t.live || {};
              if (live.steps && live.steps.length) {
                turn.steps = live.steps;
                const last = live.steps[live.steps.length - 1];
                turn.stage = "正在「" + (last.label || "查数据") + "」…";
              }
              // 分支实时状态：并行时能看到有几路在查
              if (live.branches && live.branches.length) {
                turn.branches = live.branches;
              }
              if (t.status === "done") {
                const res = t.result || {};
                turn.answer = res.answer || "";
                turn.steps = res.steps || turn.steps;
                turn.evidence = res.evidence || [];
                // 阶段契约：验收环节的判定结果（对标 Lancer PROCESS.json）
                turn.contract = res.contract || {};
                // 分支树：主代理派的子代理各查各的
                turn.branches = res.branches || [];
                turn.cost = t.cost || 0;
                turn.pending = false;
                if (res.model) model.value = res.model;
                busy.value = false;
                resolve();
                return;
              }
              if (t.status === "error") {
                turn.pending = false;
                turn.error = t.error || "任务失败";
                busy.value = false;
                reject(new Error(turn.error));
                return;
              }
              pollTimer = setTimeout(tick, 900);
            } catch (e) {
              const turn = turns.value[ti];
              if (turn) {
                turn.pending = false;
                turn.error = (e && e.message) || String(e);
              }
              busy.value = false;
              reject(e);
            }
          };
          tick();
        });
      }

      /* ---------------------------------------------------------------- 历史 */
      async function toggleHistory() {
        histOpen.value = !histOpen.value;
        if (histOpen.value) await loadHistory();
      }

      async function loadHistory() {
        histLoading.value = true;
        try {
          const r = await M.api.get("/api/runs", { limit: 30 });
          history.value = (r && r.runs) || [];
        } catch (e) {
          history.value = [];
        }
        histLoading.value = false;
      }

      async function openHistory(h) {
        if (histOpenId.value === h.id) {
          // 再点一次收起
          histOpenId.value = "";
          histDetail.value = null;
          return;
        }
        histOpenId.value = h.id;
        histDetail.value = null;
        try {
          const d = await M.api.get("/api/runs/detail", { id: h.id });
          histDetail.value = d;
        } catch (e) {
          histDetail.value = { answer: "读取失败：" + ((e && e.message) || e) };
        }
      }

      async function delHistory(h) {
        try {
          await M.api.get("/api/runs/delete", { id: h.id });
        } catch (e) { /* 删不掉就当没删，列表刷新后自然消失 */ }
        if (histOpenId.value === h.id) {
          histOpenId.value = "";
          histDetail.value = null;
        }
        await loadHistory();
      }

      function fmtTime(ts) {
        if (!ts) return "";
        const d = new Date(ts * 1000);
        if (isNaN(d.getTime())) return "";
        const p = (n) => String(n).padStart(2, "0");
        return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate())
          + " " + p(d.getHours()) + ":" + p(d.getMinutes());
      }

      /* ---------------------------------------------------------------- SSE
       * 订阅式进度推送（迭代 2）。轮询保底，SSE 不可用时自动退回。
       *
       * 为什么要两条路并存：SSE 走长连接，遇到反向代理不缓冲、浏览器
       * 自动重连等边界情况时可能收不到事件。轮询虽然慢 0.9 秒但一定能
       * 拿到最终结果——**SSE 只影响实时性，不影响正确性**。
       * 所以：SSE 挂了就降级轮询，轮询拿到的结果一样完整。
       */
      function streamTask(tid, ti) {
        return new Promise((resolve, reject) => {
          const turn = turns.value[ti];
          if (!turn) return;
          let settled = false;
          let es = null;
          let fallbackTimer = null;

          const finishOk = (res) => {
            if (settled) return;
            settled = true;
            if (es) { try { es.close(); } catch (_) {} }
            if (fallbackTimer) clearTimeout(fallbackTimer);
            turn.answer = res.answer || "";
            turn.steps = res.steps || turn.steps;
            turn.evidence = res.evidence || [];
            // 阶段契约：验收环节的判定结果（对标 Lancer PROCESS.json）
            turn.contract = res.contract || {};
            turn.branches = res.branches || [];
            turn.cost = res.cost || 0;
            turn.pending = false;
            if (res.model) model.value = res.model;
            busy.value = false;
            resolve();
          };
          const finishErr = (msg) => {
            if (settled) return;
            settled = true;
            if (es) { try { es.close(); } catch (_) {} }
            if (fallbackTimer) clearTimeout(fallbackTimer);
            turn.pending = false;
            turn.error = msg || "任务失败";
            busy.value = false;
            reject(new Error(turn.error));
          };
          // SSE 一直没动静 → 降级轮询
          const armFallback = () => {
            if (settled) return;
            fallbackTimer = setTimeout(() => {
              if (settled) return;
              if (es) { try { es.close(); } catch (_) {} }
              es = null;
              pollTask(tid, ti).then(finishOk).catch(finishErr);
            }, 4000);
          };

          try {
            es = new EventSource("/api/insight/stream?id=" + encodeURIComponent(tid));
          } catch (e) {
            pollTask(tid, ti).then(finishOk).catch(finishErr);
            return;
          }
          armFallback();

          es.addEventListener("snapshot", (e) => {
            let d = {};
            try { d = JSON.parse(e.data); } catch (_) { return; }
            const live = d.live || {};
            if (live.steps && live.steps.length) {
              turn.steps = live.steps;
              const last = live.steps[live.steps.length - 1];
              turn.stage = "正在「" + (last.label || "查数据") + "」…";
            }
            if (live.branches && live.branches.length) turn.branches = live.branches;
          });

          es.addEventListener("delta", (e) => {
            let d = {};
            try { d = JSON.parse(e.data); } catch (_) { return; }
            if (d.kind === "step") {
              const st = d.step || {};
              turn.steps = (turn.steps || []).concat([st]);
              turn.stage = "正在「" + (st.label || "查数据") + "」…";
              if (d.branches && d.branches.length) turn.branches = d.branches;
            } else if (d.kind === "start") {
              if (d.runId) turn.runId = d.runId;
            }
          });

          es.addEventListener("done", (e) => {
            let d = {};
            try { d = JSON.parse(e.data); } catch (_) {}
            finishOk(Object.assign({}, d.result || {}, { cost: d.cost }));
          });

          es.addEventListener("error", (e) => {
            // 事件流里的 error 事件（任务真失败）vs 连接错误（onerror 无 data）
            let d = null;
            try { d = JSON.parse(e.data); } catch (_) { d = null; }
            if (d && d.error) {
              finishErr(d.error);
              return;
            }
            // 连接层错误：降级轮询
            if (settled) return;
            if (es) { try { es.close(); } catch (_) {} }
            es = null;
            pollTask(tid, ti).then(finishOk).catch(finishErr);
          });
        });
      }

      function usePreset(p) {
        question.value = p.q;
        nextTick(() => submit());
      }

      function askFollow(text) {
        question.value = text;
        nextTick(() => submit());
      }

      // 追问建议：从模型这一步查过的东西反推，比写死三个模板有用
      function followUps(t) {
        const out = [];
        const steps = t.steps || [];
        const sawTimeline = steps.some((s) => s.tool === "read_timeline");
        const sawStats = steps.some((s) => s.tool === "chat_stats");
        if (sawTimeline) out.push("那上个月呢？");
        if (sawStats) out.push("换个时间段看呢？");
        if ((t.evidence || []).length) out.push("把原话再多读几段");
        if (!out.length) out.push("再详细点", "有没有反直觉的地方");
        return out.slice(0, 3);
      }

      // 点击证据 → 展开完整原文（这才是「可核对」的意义：默认截断，点了看全）
      // 注意：展开态单独放 evOpenMap，不要和 evShown（每轮显示条数）混在
      // 一个对象里——之前两套键混用，导致点了没反应。
      function toggleEv(ti, ei) {
        const key = ti + ":" + ei;
        const next = Object.assign({}, evOpenMap.value);
        if (next[key]) {
          delete next[key];
        } else {
          next[key] = true;
        }
        evOpenMap.value = next;
      }

      function evOpen(ti, ei) {
        return !!(evOpenMap.value || {})[ti + ":" + ei];
      }

      // 证据分两类：原话（可点开看全文）vs 统计数字（SQL 算的，不是原话）。
      // 混在一起摆会让人误以为「消息量 158565 条」是某句话。
      // _i 保留原始下标，展开状态才不会错位。
      function quotes(t, ti) {
        const all = t.evidence || [];
        const picked = [];
        all.forEach((e, i) => {
          if ((e.kind || "quote") === "quote" && picked.length < (evShown.value[ti] || 6)) {
            picked.push(Object.assign({}, e, { _i: i }));
          }
        });
        return picked;
      }

      function stats(t) {
        return (t.evidence || []).filter(e => e.kind === "stat");
      }

      function autoGrow(e) {
        const el = e.target;
        el.style.height = "auto";
        el.style.height = Math.min(160, el.scrollHeight) + "px";
      }

      // ---------------------------------------------------------------- 渲染
      function rendered(t) {
        const a = t.answer || "";
        if (!a) return "";
        let html = a
          .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
          .replace(/^### (.*)$/gm, "<h4>$1</h4>")
          .replace(/^## (.*)$/gm, "<h3>$1</h3>")
          .replace(/^# (.*)$/gm, "<h3>$1</h3>")
          .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
          .replace(/`([^`]+)`/g, "<code>$1</code>");
        const blocks = html.split(/\n{2,}/);
        html = blocks.map((blk) => {
          const lines = blk.split("\n");
          if (lines.every((l) => /^\s*[-*]\s+/.test(l))) {
            return "<ul>" + lines.map((l) =>
              "<li>" + l.replace(/^\s*[-*]\s+/, "") + "</li>").join("") + "</ul>";
          }
          if (lines.every((l) => /^\s*\d+[.、]\s+/.test(l))) {
            return "<ol>" + lines.map((l) =>
              "<li>" + l.replace(/^\s*\d+[.、]\s+/, "") + "</li>").join("") + "</ol>";
          }
          if (/^<h[34]>/.test(blk)) return blk;
          return "<p>" + blk.replace(/\n/g, "<br>") + "</p>";
        }).join("");
        return html;
      }

      // ---------------------------------------------------------------- 原始统计
      async function loadRaw() {
        if (rawData.value || rawLoading.value) return;
        rawLoading.value = true;
        try {
          const p = await M.api.get("/api/profile");
          rawData.value = p || {};
        } catch (e) {
          rawData.value = null;
        } finally {
          rawLoading.value = false;
        }
      }
      watch(rawOpen, (v) => { if (v) loadRaw(); });

      const profile = computed(() => rawData.value || {});

      const heat = computed(() => profile.value.heat || {});

      const calendar = computed(() => {
        const h = heat.value;
        const days = h.days || [];
        if (!days.length) return [];
        const first = days[0].date;
        const last = days[days.length - 1].date;
        const max = Math.max(1, ...days.map((d) => d.n || 0));
        const out = [];
        const start = new Date(first + "T00:00:00");
        const end = new Date(last + "T00:00:00");
        // 先补成连续日期轴，再排网格 —— 否则空档之后星期对齐全错位
        const cur = new Date(start);
        while (cur <= end) {
          const ds = cur.toISOString().slice(0, 10);
          const hit = days.find((d) => d.date === ds);
          out.push(hit ? { date: ds, n: hit.n, level: Math.ceil(((hit.n || 0) / max) * 4) } : null);
          cur.setDate(cur.getDate() + 1);
        }
        while (out.length % 7 !== 0) out.push(null);
        return out;
      });

      const heatMonths = computed(() => {
        const out = [];
        const cells = calendar.value;
        let lastMonth = "";
        for (let i = 0; i < cells.length; i += 7) {
          const c = cells[i];
          const m = c ? c.date.slice(0, 7) : "";
          if (m && m !== lastMonth) {
            out.push(m.slice(5) + "月");
            lastMonth = m;
          }
        }
        return out;
      });

      const hourlyRows = computed(() => {
        const rh = (profile.value.rhythm || {}).hourly || [];
        const max = Math.max(1, ...rh.map((x) => x.total || 0));
        return rh.map((x) => ({
          h: x.hour, n: x.total || 0,
          pct: Math.round(((x.total || 0) / max) * 100),
        }));
      });

      const topics = computed(() => {
        const by = (profile.value.topics || {}).byTopic || [];
        const max = Math.max(1, ...by.map((x) => x.hits || 0));
        return by.map((x) => ({
          name: x.topic, count: x.hits || 0,
          pct: Math.round(((x.hits || 0) / max) * 100),
        }));
      });

      function heatColor(level) {
        const map = ["#e8f0ec", "#c3ded0", "#8cc3aa", "#4f9e7c", "#2d6a4f"];
        return map[Math.max(0, Math.min(4, level || 0))];
      }

      return {
        question, presets, turns, busy, error, cost: ref(0), model,
        libTotal, libSessions, libContacts, libSpan, topNames, ph,
        rawOpen, rawLoading, rawData, evShown, evOpenMap,
        submit, usePreset, askFollow, followUps, toggleEv, evOpen, autoGrow,
        quotes, stats,
        rendered, calendar, heatMonths, hourlyRows, topics, heatColor,
        // 历史分析
        histOpen, histLoading, history, histOpenId, histDetail,
        toggleHistory, loadHistory, openHistory, delHistory, fmtTime,
        M,
      };
    },
  };

  global.MosuV2 = global.MosuV2 || {};
  global.MosuV2.Insight = Insight;
})(window);
