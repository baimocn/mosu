/* ==========================================================================
   页面 · 洞察（统计 / 画像 / 年报）+ 通用结构化渲染器
   画像与年报的返回结构由后端算法决定、字段较深，前端不做硬编码假设：
   用递归渲染器自适应展示，任何结构都不会白屏或报错。
   ========================================================================== */
(function (global) {
  "use strict";
  const M = global.Mosu;

  /** 热力图分级：按当天占峰值的比例划 5 档，0 档留白 */
  function levelOf(c, max) {
    if (!c) return 0;
    const r = c / Math.max(1, max);
    return r > 0.66 ? 5 : r > 0.45 ? 4 : r > 0.28 ? 3 : r > 0.14 ? 2 : 1;
  }

  /* ---------------------------------------------------------------- 通用渲染 */
  const JsonView = {
    name: "JsonView",
    props: {
      data: { default: null },
      depth: { type: Number, default: 0 },
      title: { type: String, default: "" },
    },
    template: `
      <div class="jv" :style="{paddingLeft: depth ? '12px' : '0'}">
        <div v-if="title" class="card-title" style="font-size:13px">{{ title }}</div>
        <template v-if="isList">
          <div v-for="(v,i) in data" :key="i" style="margin-bottom:6px">
            <template v-if="isPrimitive(v)">
              <span class="muted tiny">{{ i+1 }}.</span> {{ v }}
            </template>
            <json-view v-else :data="v" :depth="depth+1" />
          </div>
          <div v-if="!data.length" class="muted tiny">（空）</div>
        </template>
        <dl v-else-if="isObj" class="kv">
          <template v-for="(v,k) in data" :key="k">
            <dt>{{ label(k) }}</dt>
            <dd>
              <template v-if="isPrimitive(v)">{{ v }}</template>
              <json-view v-else :data="v" :depth="depth+1" />
            </dd>
          </template>
        </dl>
        <div v-else>{{ data }}</div>
      </div>`,
    computed: {
      isList() { return Array.isArray(this.data); },
      isObj() { return this.data && typeof this.data === "object" && !Array.isArray(this.data); },
    },
    methods: {
      isPrimitive(v) {
        return v === null || v === undefined
          || ["string", "number", "boolean"].includes(typeof v);
      },
      label(k) {
        const map = {
          name: "名称", nickname: "昵称", remark: "备注", wxid: "微信 ID",
          sessions: "会话", messages: "消息", messageCount: "消息数",
          keywords: "高频词", topics: "话题", traits: "特征", summary: "摘要",
          style: "风格", emotion: "情感", sentiment: "情感倾向", score: "分值",
          activeHours: "活跃时段", peakHour: "最活跃小时", firstTime: "首次",
          lastTime: "最近", year: "年份", total: "合计", ratio: "占比",
          count: "次数", word: "词", replyLatency: "回复时延",
        };
        return map[k] || k;
      },
    },
  };

  /* ---------------------------------------------------------------- 统计 */
  const Stats = {
    components: { JsonView },
    template: `
    <div class="page">
      <div v-if="loading" class="grid grid-4">
        <div v-for="i in 4" class="skeleton" :key="i" style="height:96px"></div>
      </div>
      <template v-else-if="g">
        <div class="grid grid-4">
          <div class="tile rise">
            <div class="tile-head">消息总数</div>
            <div class="tile-value">{{ fmtNum(g.totalMessages) }}</div>
            <div class="tile-foot">
              <span class="trend trend-flat">{{ fmtNum(g.totalSessions) }} 个会话</span>
            </div>
          </div>
          <div class="tile rise">
            <div class="tile-head">联系人</div>
            <div class="tile-value">{{ fmtNum(g.totalContacts) }}</div>
            <div class="tile-foot">去重后的往来对象</div>
          </div>
          <div class="tile rise">
            <div class="tile-head">最活跃一天</div>
            <div class="tile-value" style="font-size:20px">{{ peakDayText }}</div>
            <div class="tile-foot">{{ peakDayCount }}</div>
          </div>
          <div class="tile rise">
            <div class="tile-head">时间跨度</div>
            <div class="tile-value" style="font-size:15px;line-height:1.35">
              {{ (g.timeRange && g.timeRange.firstText) || '—' }}
              <span class="muted">→</span>
              {{ (g.timeRange && g.timeRange.lastText) || '—' }}
            </div>
            <div class="tile-foot">{{ spanDays ? ('跨越 ' + fmtNum(spanDays) + ' 天') : '' }}</div>
          </div>
        </div>

        <div class="card" style="margin-top:16px">
          <div class="card-title">
            活跃度日历
            <div class="spacer"></div>
            <span class="tiny muted" v-if="heatLoading">正在按天聚合…</span>
          </div>
          <div class="card-sub">
            最近一年每天的消息密度。颜色越深，那天聊得越多。
          </div>
          <div v-if="heatLoading" class="skeleton" style="height:110px"></div>
          <template v-else-if="heatCells.length">
            <div class="heat">
              <div style="display:inline-block">
                <div class="heat-months">
                  <div v-for="(m,i) in heatMonths" :key="i" style="position:relative;width:13px">
                    <span v-if="m" style="position:absolute;left:0;top:0;white-space:nowrap">{{ m }}</span>
                  </div>
                </div>
                <div class="heat-grid">
                  <div v-for="(c,i) in heatCells" :key="i"
                       class="heat-cell" :class="'heat-l' + c.level"
                       :title="c.title"></div>
                </div>
              </div>
            </div>
            <div class="heat-legend">
              <span>少</span>
              <span class="heat-cell"></span>
              <span class="heat-cell heat-l1"></span>
              <span class="heat-cell heat-l2"></span>
              <span class="heat-cell heat-l3"></span>
              <span class="heat-cell heat-l4"></span>
              <span class="heat-cell heat-l5"></span>
              <span>多</span>
              <div class="spacer" style="flex:1"></div>
              <span>活跃 {{ fmtNum(activeDays) }} 天 · 最长连续 {{ fmtNum(maxStreak) }} 天</span>
            </div>
          </template>
          <div v-else class="muted tiny">这段时间没有可统计的消息。</div>
        </div>

        <div class="grid grid-2" style="margin-top:16px">
          <div class="card">
            <div class="card-title">消息类型分布</div>
            <div v-for="(v,k) in g.byType" :key="k" class="bar-row">
              <div class="bar-label">{{ typeName(k) }}</div>
              <div class="bar-track">
                <div class="bar-fill" :style="{width: pct(v, maxType)}"></div>
              </div>
              <div class="bar-value">{{ fmtNum(v) }}</div>
            </div>
          </div>

          <div class="card">
            <div class="card-title">消息最多的会话</div>
            <div v-for="c in contacts.slice(0,12)" :key="c.wxid" class="bar-row">
              <div class="bar-label" :title="c.displayName">{{ c.displayName }}</div>
              <div class="bar-track">
                <div class="bar-fill" :style="{width: pct(c.messageCount, maxContact)}"></div>
              </div>
              <div class="bar-value">{{ fmtNum(c.messageCount) }}</div>
            </div>
          </div>
        </div>

        <div class="card" style="margin-top:16px">
          <div class="card-title">
            时间分布
            <div class="spacer"></div>
            <button class="chip" :class="{active: by==='hour'}" @click="switchBy('hour')">按小时</button>
            <button class="chip" :class="{active: by==='weekday'}" @click="switchBy('weekday')">按星期</button>
            <button class="chip" :class="{active: by==='month'}" @click="switchBy('month')">按月</button>
          </div>
          <div v-if="!time" class="skeleton" style="height:120px"></div>
          <div v-else>
            <div v-for="it in time.items" :key="it.key || it.bucket" class="bar-row">
              <div class="bar-label">{{ it.bucket }}</div>
              <div class="bar-track">
                <div class="bar-fill" :style="{width: pct(it.count, maxTime)}"></div>
              </div>
              <div class="bar-value">{{ fmtNum(it.count) }}</div>
            </div>
          </div>
        </div>

        <div class="card" style="margin-top:16px">
          <div class="card-title">
            全库高频词
            <div class="spacer"></div>
            <button class="btn btn-sm" @click="runKeywords" :disabled="kwRunning">
              <span v-if="kwRunning" class="spinner"></span>
              {{ kwRunning ? '分析中…' : '开始分析' }}
            </button>
          </div>
          <div v-if="kwRunning" class="progress progress-bar indeterminate"
               style="margin-bottom:8px"></div>
          <div v-if="keywords.length" style="display:flex;flex-wrap:wrap;gap:8px">
            <span v-for="k in keywords" :key="k.word" class="chip"
                  :style="{fontSize: kwSize(k.count)}">
              {{ k.word }} · {{ k.count }}
            </span>
          </div>
          <div v-else class="muted tiny">
            分词统计要扫全库（约 20 万条消息），在后台跑，完成后自动显示。
          </div>
        </div>
      </template>
      <div v-else class="empty">
        <div class="empty-icon">📊</div>
        <div class="empty-title">统计数据不可用</div>
        <div class="empty-desc">{{ err || '请先完成数据配置（密钥 + 数据目录）。' }}</div>
      </div>
    </div>`,
    data() {
      return {
        loading: true, g: null, contacts: [], time: null, by: "hour",
        keywords: [], kwRunning: false, err: "",
        heat: null, heatLoading: true,
      };
    },
    computed: {
      maxType() { return Math.max(1, ...Object.values(this.g ? this.g.byType : { x: 1 })); },
      maxContact() { return Math.max(1, ...this.contacts.map((c) => c.messageCount)); },
      maxTime() { return Math.max(1, ...(this.time ? this.time.items.map((i) => i.count) : [1])); },

      /* ---- 活跃度日历 ---- */
      heatCells() {
        const dist = (this.heat && this.heat.distribution) || {};
        if (!Object.keys(dist).length) return [];
        const end = new Date(); end.setHours(0, 0, 0, 0);
        const endDay = end.getDay();                 // 0 = 周日，网格按列填充，必须从周日起步
        const cur = new Date(end);
        cur.setDate(end.getDate() - endDay - 364);
        const cells = [];
        const pad = (n) => (n < 10 ? "0" + n : String(n));
        const top = Math.max(1, ...Object.values(dist).map(Number));
        for (let i = 0; i <= 364 + endDay; i++) {
          const key = `${cur.getFullYear()}-${pad(cur.getMonth() + 1)}-${pad(cur.getDate())}`;
          const c = dist[key] || 0;
          cells.push({
            key, count: c, level: levelOf(c, top),
            month: cur.getMonth() + 1 + "月",
            title: c ? `${key} · ${c} 条` : `${key} · 无消息`,
          });
          cur.setDate(cur.getDate() + 1);
        }
        return cells;
      },
      heatMax() {
        const dist = (this.heat && this.heat.distribution) || {};
        return Math.max(1, ...Object.values(dist).map(Number));
      },
      heatMonths() {
        /* 只在「该列包含 1 号」的列标注月份，一个月最多出现一次，避免重叠 */
        const out = [];
        for (let i = 0; i < this.heatCells.length; i += 7) {
          const first = this.heatCells.slice(i, i + 7).find((c) => c.key.endsWith("-01"));
          out.push(first ? first.month : "");
        }
        return out;
      },
      activeDays() { return this.heatCells.filter((c) => c.count > 0).length; },
      maxStreak() {
        let best = 0, run = 0;
        for (const c of this.heatCells) {
          if (c.count > 0) { run++; if (run > best) best = run; }
          else run = 0;
        }
        return best;
      },
      peakDay() {
        const dist = (this.heat && this.heat.distribution) || {};
        let k = "", v = 0;
        for (const [key, n] of Object.entries(dist)) { if (n > v) { v = n; k = key; } }
        return { key: k, count: v };
      },
      peakDayText() { return this.peakDay.key || "—"; },
      peakDayCount() { return this.peakDay.count ? `${M.fmtNum(this.peakDay.count)} 条` : ""; },
      spanDays() {
        const r = this.g && this.g.timeRange;
        if (!r || !r.first || !r.last) return 0;
        return Math.max(0, Math.round((r.last - r.first) / 86400));
      },
    },
    methods: {
      ...{ fmtNum: M.fmtNum },
      typeName(k) {
        return { text: "文本", image: "图片", voice: "语音", video: "视频",
                 file: "文件", emoji: "表情", system: "系统", link: "链接" }[k] || k;
      },
      pct(v, max) { return Math.max(2, (v / Math.max(1, max)) * 100) + "%"; },
      kwSize(c) {
        const mx = Math.max(1, ...this.keywords.map((k) => k.count));
        return (12 + Math.round((c / mx) * 8)) + "px";
      },
      async switchBy(by) {
        this.by = by; this.time = null;
        this.time = await M.api.get("/api/stats/time", { by });
      },
      /** 逐日聚合要扫全库，单独加载，不拖慢首屏 */
      async loadHeat() {
        this.heatLoading = true;
        try { this.heat = await M.api.get("/api/stats/time", { by: "date" }); }
        catch (e) { this.heat = null; }
        finally { this.heatLoading = false; }
      },
      async runKeywords() {
        this.kwRunning = true;
        try {
          const d = await M.api.post("/api/stats/keywords", { top: 60 });
          const tid = d.id || (d.task && d.task.id);
          if (!tid) { this.keywords = d.keywords || []; return; }
          for (let i = 0; i < 300; i++) {
            await new Promise((r) => setTimeout(r, 1500));
            const t = await M.api.get("/api/task", { id: tid });
            if (t.status === "done") {
              this.keywords = (t.result && t.result.keywords) || [];
              M.toast.ok("高频词分析完成");
              break;
            }
            if (t.status === "error") { M.toast.err("分析失败：" + t.error); break; }
          }
        } catch (e) { M.toast.err("分析失败：" + e.message); }
        finally { this.kwRunning = false; }
      },
    },
    async mounted() {
      try {
        const [g, c, t] = await Promise.all([
          M.api.get("/api/stats/global"),
          M.api.get("/api/stats/contacts", { top: 20 }),
          M.api.get("/api/stats/time", { by: "hour" }),
        ]);
        this.g = g; this.contacts = c.contacts || []; this.time = t;
      } catch (e) { this.err = e.message; }
      finally { this.loading = false; }
      this.loadHeat();
    },
  };

  /* ---------------------------------------------------------------- 画像 / 年报 */
  const Insight = {
    components: { JsonView },
    props: { kind: { type: String, default: "profile" } },   // profile | report
    template: `
    <div class="page">
      <div class="row" style="margin-bottom:16px">
        <button class="chip" :class="{active: mode==='self'}" @click="setMode('self')"
                v-if="kind==='profile'">我的画像</button>
        <button class="chip" :class="{active: mode==='contact'}" @click="setMode('contact')"
                v-if="kind==='profile'">联系人画像</button>
        <select class="select" style="width:280px" v-if="mode==='contact'"
                v-model="picked" @change="load">
          <option value="">选择一个会话…</option>
          <option v-for="s in sessions" :key="s.sessionId" :value="s.sessionId">
            {{ s.displayName }}
          </option>
        </select>
        <select class="select" style="width:120px" v-if="kind==='report'"
                v-model="year" @change="load">
          <option value="">全部年份</option>
          <option v-for="y in years" :key="y" :value="y">{{ y }} 年</option>
        </select>
        <div class="spacer"></div>
        <button class="btn btn-sm" @click="load" :disabled="loading">
          <span v-if="loading" class="spinner"></span>重新生成
        </button>
      </div>

      <div v-if="loading" class="grid grid-2">
        <div class="skeleton" style="height:200px"></div>
        <div class="skeleton" style="height:200px"></div>
      </div>
      <div v-else-if="err" class="empty">
        <div class="empty-icon">🧭</div>
        <div class="empty-title">生成失败</div>
        <div class="empty-desc">{{ err }}</div>
      </div>
      <div v-else-if="data">
        <!-- 顶层是对象：按卡片分块；是数组：直接渲染 -->
        <div class="grid grid-2">
          <div v-for="(v,k) in topBlocks" :key="k" class="card">
            <json-view :data="v" :title="label(k)" />
          </div>
        </div>
        <div v-if="topRest && Object.keys(topRest).length" class="card" style="margin-top:16px">
          <json-view :data="topRest" title="其他维度" />
        </div>
      </div>
      <div v-else class="empty">
        <div class="empty-icon">🧭</div>
        <div class="empty-title">{{ kind==='profile' ? '选择一个会话生成画像' : '生成你的微信年报' }}</div>
        <div class="empty-desc">全部在本机完成，不上传任何数据。</div>
      </div>
    </div>`,
    data() {
      return {
        mode: "self", picked: "", year: "", years: [], sessions: [],
        data: null, loading: false, err: "",
      };
    },
    computed: {
      topBlocks() {
        if (!this.data || Array.isArray(this.data)) return {};
        // 前 6 个「复杂值」单独成卡，其余合并，避免几十张卡片刷屏
        const entries = Object.entries(this.data)
          .filter(([, v]) => v && typeof v === "object").slice(0, 6);
        return Object.fromEntries(entries);
      },
      topRest() {
        if (!this.data || Array.isArray(this.data)) return null;
        const used = new Set(Object.keys(this.topBlocks));
        const rest = {};
        for (const [k, v] of Object.entries(this.data)) {
          if (used.has(k)) continue;
          if (v && typeof v === "object") continue;
          rest[k] = v;
        }
        return rest;
      },
    },
    methods: {
      label(k) {
        return { basic: "基本信息", stats: "互动统计", style: "语言风格",
                 topics: "话题偏好", emotion: "情绪特征", keywords: "高频词",
                 timeline: "时间线", highlights: "年度亮点", summary: "总览",
                 monthly: "月度分布", contacts: "关系", profile: "画像" }[k] || k;
      },
      setMode(m) {
        this.mode = m;
        if (m === "self") { this.picked = ""; this.load(); }
        else { this.data = null; }
      },
      async load() {
        if (this.kind === "profile" && this.mode === "contact" && !this.picked) return;
        this.loading = true; this.err = "";
        try {
          if (this.kind === "profile") {
            this.data = await M.api.get("/api/profile",
              this.picked ? { session: this.picked } : {});
          } else {
            this.data = await M.api.get("/api/report", this.year ? { year: this.year } : {});
          }
        } catch (e) { this.err = e.message; this.data = null; }
        finally { this.loading = false; }
      },
    },
    async mounted() {
      const now = new Date().getFullYear();
      this.years = Array.from({ length: 8 }, (_, i) => now - i);
      try {
        const d = await M.api.get("/api/sessions", { limit: 300 });
        this.sessions = d.sessions || [];
      } catch (e) { /* 忽略 */ }
      if (this.kind === "report") this.load();
      else this.load();
    },
  };

  global.MosuViews = global.MosuViews || {};
  Object.assign(global.MosuViews, { JsonView, Stats, Insight });
})(window);
