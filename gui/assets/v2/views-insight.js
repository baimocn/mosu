/* ==========================================================================
   墨溯 v2 · 洞察工作台（以大模型为核心）
   --------------------------------------------------------------------------
   旧形态：三个 tab 铺满统计图表，用户自己看图找结论。
   新形态：用户提问 → 大模型基于「已核对事实 + 语料样本」回答，
           图表降级为可展开的「支撑数据」，只作为答案的佐证。

   为什么这样改：
     - 看图找结论要求用户具备读图能力并自己提出问题，成本高
     - 大模型能把「24 万条消息」压成一句话结论，且能指出反直觉之处
     - 但模型自己算数会编数字 → 所有数字由后端算好并标注口径，
       模型只做转述与解读，答案里必须带「依据」
   ========================================================================== */
(function (global) {
  "use strict";
  const M = global.Mosu;
  const { ref, computed, onMounted, nextTick, watch } = Vue;

  const Insight = {
    props: { route: { type: String, default: "insight" } },
    template: `
    <div class="v2-page v2-scroll v2-ia">
      <div class="v2-page-head">
        <div>
          <h1 class="v2-page-title">洞察</h1>
          <p class="v2-page-desc">
            问一个问题，大模型基于你本机的
            <strong>{{ M.fmtNum(libTotal) }}</strong> 条聊天记录回答。
            数字全部由数据库算出并标注口径，模型只负责解读。
          </p>
        </div>
        <div v-if="model" class="v2-ia-model">
          <span class="v2-ia-model-dot"></span>{{ model }}
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
        <div v-if="!answered && presets.length" class="v2-ia-presets">
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
        <div v-if="!answered && libTotal" class="v2-ia-scope">
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

      <!-- ============ 状态 ============ -->
      <div v-if="busy" class="v2-ia-status">
        <div class="v2-ia-status-row">
          <span class="v2-spin"></span>
          <span>{{ stageText }}</span>
        </div>
        <div class="v2-ia-progress"><div class="v2-ia-progress-bar"></div></div>
        <div class="v2-ia-hint">
          全库 {{ M.fmtNum(libTotal) }} 条消息要重新扫一遍，加上模型推理，约 20–40 秒。
        </div>
      </div>

      <div v-else-if="error" class="v2-note danger">{{ error }}</div>

      <!-- ============ 回答 ============ -->
      <div v-else-if="result" class="v2-ia-answer-wrap">
        <div class="v2-ia-q-echo">
          <span class="v2-ia-q-mark">问</span>{{ result.question }}
        </div>

        <article class="v2-ia-answer" v-html="rendered"></article>

        <!-- 依据 -->
        <div v-if="citedFacts.length" class="v2-ia-cites">
          <div class="v2-ia-sec-title">答案引用了这些依据</div>
          <div class="v2-ia-cite-row">
            <span v-for="(c, i) in citedFacts" :key="i" class="v2-ia-cite" :title="c.full">{{ c.label }}</span>
          </div>
        </div>

        <!-- 支撑数据：折叠抽屉 -->
        <div class="v2-ia-evidence">
          <button class="v2-ia-ev-toggle" @click="evOpen = !evOpen">
            <span class="v2-ia-ev-caret" :class="{open: evOpen}">›</span>
            支撑数据
            <span class="v2-ia-ev-count">
              {{ (result.facts || []).length }} 条事实 ·
              {{ (result.samples || []).length }} 条样本
            </span>
          </button>

          <div v-if="evOpen" class="v2-ia-ev-body">
            <div class="v2-ia-sec-title">已核对事实（数字来自数据库，可回溯）</div>
            <ul class="v2-ia-facts">
              <li v-for="(f, i) in (result.facts || [])" :key="i">{{ f }}</li>
            </ul>

            <div v-if="(result.samples || []).length" class="v2-ia-sec-title" style="margin-top:18px">
              语料样本（真实聊天片段，用于判断语气，不是答案素材）
            </div>
            <div v-if="(result.samples || []).length" class="v2-ia-samples">
              <div v-for="(s, i) in result.samples" :key="i" class="v2-ia-sample">
                <div class="v2-ia-sample-meta">
                  <span class="v2-ia-sample-who">{{ s.speaker }}</span>
                  <span v-if="s.time" class="v2-ia-sample-time">{{ s.time }}</span>
                </div>
                <div class="v2-ia-sample-text">{{ s.text }}</div>
              </div>
            </div>

            <button class="v2-btn v2-ia-again" @click="evOpen = false">收起</button>
          </div>
        </div>

        <div class="v2-ia-foot">
          <span>用时 {{ cost }} 秒</span>
          <button class="v2-btn" @click="askAgain">换个问题</button>
        </div>
      </div>

      <!-- ============ 原始数据（证据的另一面） ============ -->
      <div class="v2-ia-raw">
        <button class="v2-ia-ev-toggle" @click="rawOpen = !rawOpen">
          <span class="v2-ia-ev-caret" :class="{open: rawOpen}">›</span>
          原始统计图表
          <span class="v2-ia-ev-count">活跃度日历 · 时段分布 · 话题分布</span>
        </button>

        <div v-if="rawOpen" class="v2-ia-ev-body">
          <div v-if="rawLoading" class="v2-ia-hint">正在统计…</div>
          <template v-else>
            <!-- 日历 -->
            <div class="v2-ia-sec-title">活跃度日历</div>
            <p class="v2-ia-sec-desc">
              每天的消息数，颜色越深越活跃。共 {{ heatDays }} 天有记录。
            </p>
            <div class="v2-heat-wrap" v-if="calendar.length">
              <div class="v2-heat-inner">
                <div class="v2-heat-months" :style="{width: heatWidth + 'px'}">
                  <span v-for="m in heatMonths" :key="m.key"
                        :style="{left: m.left + 'px'}">{{ m.label }}</span>
                </div>
                <div class="v2-heat">
                  <div v-for="(c,i) in calendar" :key="i"
                       class="v2-heat-cell"
                       :style="c ? {background: heatColor(c.level)} : {background:'transparent'}"
                       :title="c ? (c.date + ' · ' + c.n + ' 条') : ''"></div>
                </div>
              </div>
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
        </div>
      </div>
    </div>
    `,

    setup(props) {
      const question = ref("");
      const presets = ref([]);
      const result = ref(null);
      const busy = ref(false);
      const error = ref("");
      const cost = ref(0);
      const evOpen = ref(false);
      const rawOpen = ref(false);
      const rawLoading = ref(false);
      const model = ref("");
      const libTotal = ref(0);
      const libSessions = ref(0);
      const libContacts = ref(0);
      const libSpan = ref("");
      const topNames = ref([]);

      // 原始统计
      const profile = ref(null);
      const heat = ref({});
      const stageText = ref("正在扫描全部消息…");

      const ph = computed(() => {
        const n = libTotal.value;
        if (!n) return "问我任何关于你聊天记录的事…";
        return `问我任何关于这 ${M.fmtNum(n)}条聊天的事，比如：我什么时候最话多？`;
      });

      let pollTimer = null;

      onMounted(async () => {
        // 从命令面板进来「原始统计图表」→ 直接展开
        if (props.route === "insight.raw") rawOpen.value = true;
        // 预设问题
        try {
          const p = await M.api.get("/api/insight/presets");
          presets.value = (p && p.presets) || [];
        } catch (e) {
          presets.value = [];
        }
        // 库概况（轻量接口，用于首屏信息带）
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
        // 聊得最多的几个（轻量接口）
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
        result.value = null;
        evOpen.value = false;
        stageText.value = "正在扫描全部消息…";
        try {
          const r = await M.api.post("/api/insight/ask", { question: q });
          await pollTask(r.taskId);
        } catch (e) {
          error.value = (e && e.message) || String(e);
          busy.value = false;
        }
      }

      function pollTask(tid) {
        return new Promise((resolve, reject) => {
          const tick = async () => {
            try {
              const t = await M.api.get("/api/task", { id: tid });
              if (!t) throw new Error("任务丢失");
              if (t.status === "done") {
                result.value = t.result || {};
                cost.value = t.cost || 0;
                model.value = (t.result && t.result.model) || model.value;
                busy.value = false;
                resolve();
                return;
              }
              if (t.status === "error") {
                throw new Error(t.error || "任务失败");
              }
              // 阶段提示：跑得越久越可能已经进 LLM 阶段
              const c = t.cost || 0;
              if (c === 0) stageText.value = "正在扫描全部消息…";
              else if (c < 12) stageText.value = "正在扫描全部消息…";
              else stageText.value = "模型正在思考…";
              pollTimer = setTimeout(tick, 1200);
            } catch (e) {
              error.value = (e && e.message) || String(e);
              busy.value = false;
              reject(e);
            }
          };
          tick();
        });
      }

      function usePreset(p) {
        question.value = p.q;
        nextTick(() => submit());
      }

      function askAgain() {
        result.value = null;
        question.value = "";
        nextTick(() => {
          const el = document.querySelector(".v2-ia-input");
          if (el) el.focus();
        });
      }

      function autoGrow(e) {
        const el = e.target;
        el.style.height = "auto";
        el.style.height = Math.min(160, el.scrollHeight) + "px";
      }

      // ---------------------------------------------------------------- 渲染
      const answered = computed(() => !!result.value);

      // 依据：优先用模型自己标的；模型没标时按事实清单关键词兜底
      // chip 只放短标签，完整原句挂在 title 上（悬停可见）——避免整段事实塞进一个泡里
      const citedFacts = computed(() => {
        const r = result.value;
        if (!r) return [];
        const facts = r.facts || [];
        let cites = (r.citations || []).slice();
        if (!cites.length) {
          // 兜底：拿答案里出现的关键短语去事实清单里找对应项
          const ans = r.answer || "";
          const keys = [
            ["消息量最多的联系人", "联系人"],
            ["高频词", "词"],
            ["时段", "点"],
            ["深夜", "深夜"],
            ["消息构成", "文本"],
            ["你发出", "你发"],
            ["时间跨度", "跨度"],
          ];
          facts.forEach((f) => {
            if (cites.length >= 4) return;
            for (const [label, hint] of keys) {
              if (f.includes(label) && ans.includes(hint)) {
                cites.push(f);
                break;
              }
            }
          });
        }
        // 去重 + 抽短标签
        const seen = new Set();
        const out = [];
        cites.forEach((full) => {
          const t = String(full || "").trim();
          if (!t || seen.has(t)) return;
          seen.add(t);
          out.push({ full: t, label: shortLabel(t) });
        });
        return out.slice(0, 8);
      });

      // 从一句话里抽一个能当标签的短句
      function shortLabel(t) {
        // 优先取「标签：值」形式的中文冒号前半段（半角冒号多见于时间，别乱切）
        const cn = t.indexOf("：");
        if (cn > 0 && cn <= 20) return t.slice(0, cn);
        // 否则截到第一个逗号/顿号
        const cut = t.split(/[，,、；;]/)[0];
        if (cut.length <= 16) return cut;
        // 别切在括号中间——回退到配对闭合处
        const open = Math.max(cut.lastIndexOf("（"), cut.lastIndexOf("("));
        if (open > 0) {
          const close = cut.indexOf(open === cut.lastIndexOf("（") ? "）" : ")", open);
          if (close > 0 && close + 1 <= 18) return cut.slice(0, close + 1);
        }
        return cut.slice(0, 16) + "…";
      }

      const rendered = computed(() => {        const a = (result.value && result.value.answer) || "";
        if (!a) return "";
        // 极简 markdown：**粗体** 与段落
        let html = a
          .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
          .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
          .replace(/`([^`]+)`/g, "<code>$1</code>");
        html = html.split(/\n{2,}/).map((p) => {
          const t = p.trim();
          if (!t) return "";
          if (/^###\s/.test(t)) return "<h4>" + t.replace(/^###\s*/, "") + "</h4>";
          if (/^[-*]\s/.test(t)) {
            const items = t.split(/\n/).map((li) => {
              const s = li.trim().replace(/^[-*]\s*/, "");
              return s ? "<li>" + s + "</li>" : "";
            }).join("");
            return "<ul>" + items + "</ul>";
          }
          if (/^\d+\.\s/.test(t)) {
            const items = t.split(/\n/).map((li) => {
              const s = li.trim().replace(/^\d+\.\s*/, "");
              return s ? "<li>" + s + "</li>" : "";
            }).join("");
            return "<ol>" + items + "</ol>";
          }
          return "<p>" + t.replace(/\n/g, "<br/>") + "</p>";
        }).join("");
        return html;
      });

      // ---------------------------------------------------------------- 原始统计
      const calendar = computed(() => {
        const dist = heat.value || {};
        const entries = Object.entries(dist).sort((a, b) => a[0].localeCompare(b[0]));
        if (!entries.length) return [];
        const counts = entries.map(([, n]) => n);
        const max = Math.max.apply(null, counts) || 1;
        // 后端只给「有消息的日期」，中间可能缺整段 → 先补成连续日期轴，
        // 否则空档之后的星期对齐全错位
        const first = new Date(entries[0][0] + "T00:00:00");
        const last = new Date(entries[entries.length - 1][0] + "T00:00:00");
        const map = new Map(entries);
        const span = Math.round((last - first) / 86400000);
        const days = [];
        for (let i = 0; i <= span; i++) {
          const d = new Date(first.getTime() + i * 86400000);
          const ds = d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") +
            "-" + String(d.getDate()).padStart(2, "0");
          const n = map.get(ds) || 0;
          days.push({
            date: ds, n,
            level: n === 0 ? 0 : Math.min(5, Math.ceil(n / max * 5)),
          });
        }
        const lead = first.getDay();
        const cells = [];
        for (let i = 0; i < lead; i++) cells.push(null);
        days.forEach((c) => cells.push(c));
        const rem = cells.length % 7;
        if (rem) for (let i = 0; i < 7 - rem; i++) cells.push(null);
        return cells;
      });

      const heatDays = computed(() => {
        const dist = heat.value || {};
        return Object.values(dist).filter((n) => Number(n) > 0).length;
      });

      const HEAT_COL_W = 14;
      const heatWidth = computed(() => Math.ceil(calendar.value.length / 7) * HEAT_COL_W - 3);

      const heatMonths = computed(() => {
        const cells = calendar.value;
        if (!cells.length) return [];
        const out = [];
        let lastKey = "";
        cells.forEach((c, i) => {
          if (!c) return;
          const d = new Date(c.date + "T00:00:00");
          const key = d.getFullYear() + "-" + (d.getMonth() + 1);
          if (key === lastKey) return;
          lastKey = key;
          const left = Math.floor(i / 7) * HEAT_COL_W;
          if (out.length && left - out[out.length - 1].left < 28) return;
          out.push({ key: key, left: left, label: (d.getMonth() + 1) + "月" });
        });
        return out;
      });

      const hourlyRows = computed(() => {
        const rh = (profile.value && profile.value.rhythm) || {};
        const list = rh.hourly || [];
        // 后端 hourly 项形如 { hour, total, mine }
        const max = list.reduce((a, x) => Math.max(a, x.total || 0), 0) || 1;
        return list.map((x) => ({
          h: x.hour, n: x.total || 0,
          pct: Math.round((x.total || 0) / max * 100),
        }));
      });

      const topics = computed(() => {
        const tp = (profile.value && profile.value.topics) || {};
        const list = tp.mine || tp.byTopic || [];
        const max = list.reduce((a, x) => Math.max(a, x.hits || x.count || 0), 0) || 1;
        return list.slice(0, 10).map((x) => {
          const c = x.hits || x.count || 0;
          return { name: x.topic || x.name, count: c, pct: Math.round(c / max * 100) };
        });
      });

      function heatColor(level) {
        const map = ["var(--heat-0)", "var(--heat-1)", "var(--heat-2)",
          "var(--heat-3)", "var(--heat-4)", "var(--heat-5)"];
        return map[level] || map[0];
      }

      // 展开「原始统计」时才去拉数据（重接口，不占首屏）
      async function loadRaw() {
        if (profile.value) return;
        rawLoading.value = true;
        try {
          const [p, h] = await Promise.allSettled([
            M.api.get("/api/profile"),
            M.api.get("/api/stats/time", { by: "date" }),
          ]);
          if (p.status === "fulfilled" && p.value) profile.value = p.value;
          if (h.status === "fulfilled" && h.value) heat.value = h.value.distribution || {};
        } catch (e) {
          /* 图表是补充，失败不打扰主流程 */
        } finally {
          rawLoading.value = false;
        }
      }
      watch(rawOpen, (open) => { if (open) loadRaw(); });
      // 从「原始统计图表」入口直接进来时，rawOpen 初始已是 true，watch 不触发
      if (props.route === "insight.raw") loadRaw();

      return {
        question, presets, result, busy, error, cost, evOpen, rawOpen,
        rawLoading, model, libTotal, libSessions, libContacts, libSpan,
        topNames, ph, stageText, answered, citedFacts,
        rendered, calendar, heatDays, heatWidth, heatMonths,
        hourlyRows, topics, heatColor,
        submit, usePreset, askAgain, autoGrow, M,
      };
    },
  };

  global.MosuV2 = global.MosuV2 || {};
  global.MosuV2.Insight = Insight;
})(window);
