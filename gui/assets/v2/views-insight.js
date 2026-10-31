/* ==========================================================================
   墨溯 v2 · 洞察工作台
   合并「数据统计」「画像」「年报」三页。
   关键改进：一次 /api/profile + /api/report 请求，三个视图共享同一份数据，
   切 tab 不重复拉取，也不重复画同样的 KPI 卡。
   ========================================================================== */
(function (global) {
  "use strict";
  const M = global.Mosu;
  const { ref, computed, onMounted } = Vue;

  const TABS = [
    { id: "overview", name: "总览" },
    { id: "profile", name: "我的画像" },
    { id: "report", name: "年度总结" },
  ];

  const Insight = {
    props: { route: { type: String, default: "insight" } },
    template: `
    <div class="v2-page v2-scroll">
      <div class="v2-narr">
        <div class="v2-page-head" style="display:flex;align-items:flex-start;justify-content:space-between;gap:16px">
          <div>
            <h1 class="v2-page-title">洞察</h1>
            <p class="v2-page-desc">
              从 {{ M.fmtNum(total) }} 条消息里读出的事实。只算已解密入库的部分，
              不猜测、不推断。
            </p>
          </div>
          <button class="v2-btn" @click="reload" :disabled="loading">
            {{ loading ? '分析中…' : '重新计算' }}
          </button>
        </div>

        <div class="v2-tabs">
          <button v-for="t in tabs" :key="t.id" class="v2-tab"
                  :class="{active: tab===t.id}" @click="tab=t.id">{{ t.name }}</button>
        </div>

        <div v-if="loading && !profile" style="display:flex;flex-direction:column;gap:16px">
          <div class="v2-card" style="display:flex;align-items:center;gap:14px">
            <div class="v2-skel" style="width:34px;height:34px;border-radius:10px;flex-shrink:0"></div>
            <div style="flex:1">
              <div style="font-size:14px;font-weight:500;color:var(--text-1)">正在扫描全部消息…</div>
              <div style="font-size:12px;color:var(--text-4);margin-top:3px">
                首次分析需解密并解压全库消息，约 5–10 秒。之后走缓存，秒开。
              </div>
            </div>
          </div>
          <div class="v2-skel" style="height:120px;border-radius:16px"></div>
          <div class="v2-skel" style="height:200px;border-radius:16px"></div>
        </div>

        <div v-else-if="error" class="v2-note danger">{{ error }}</div>

        <!-- ============ 总览 ============ -->
        <template v-else-if="tab==='overview'">
          <div class="v2-narr-lead">
            <h2 class="v2-narr-lead-title">
              {{ M.fmtNum(ov.totalMessages) }} 条消息，跨 {{ M.fmtNum(ov.activeDays) }} 天
            </h2>
            <div class="v2-narr-metrics">
              <div class="v2-metric accent">
                <div class="v2-metric-val">{{ M.fmtNum(ov.totalMessages) }}</div>
                <div class="v2-metric-label">消息总数</div>
              </div>
              <div class="v2-metric">
                <div class="v2-metric-val">{{ M.fmtNum(ov.myMessages) }}</div>
                <div class="v2-metric-label">我发出</div>
                <div class="v2-metric-sub">{{ pct(ov.myShare) }}%</div>
              </div>
              <div class="v2-metric">
                <div class="v2-metric-val">{{ M.fmtNum(ov.activeDays) }}</div>
                <div class="v2-metric-label">有消息的天</div>
              </div>
              <div class="v2-metric">
                <div class="v2-metric-val">{{ M.fmtNum(ov.spanDays) }}</div>
                <div class="v2-metric-label">跨度（天）</div>
              </div>
            </div>
          </div>

          <div class="v2-narr-block">
            <h3 class="v2-narr-block-title">活跃度日历</h3>
            <p class="v2-narr-block-desc">
              每天的消息数，颜色越深越活跃。共 {{ heatDays }} 天有记录。
            </p>
            <div class="v2-card">
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
              <div v-else class="v2-empty" style="padding:24px">
                <div class="v2-empty-desc">暂无日历数据</div>
              </div>
              <div style="display:flex;align-items:center;gap:6px;margin-top:12px;font-size:11px;color:var(--text-4)">
                <span>少</span>
                <span v-for="l in [0,1,2,3,4,5]" :key="l"
                      :style="{width:'11px',height:'11px',borderRadius:'3px',background:heatColor(l)}"></span>
                <span>多</span>
              </div>
            </div>
          </div>

          <div class="v2-narr-cols">
            <div class="v2-narr-block">
              <h3 class="v2-narr-block-title">消息构成</h3>
              <p class="v2-narr-block-desc">按消息类型拆分。</p>
              <div class="v2-card">
                <div class="v2-bar-row" v-for="(c,i) in kindRows" :key="i">
                  <span class="v2-bar-label">{{ c.name }}</span>
                  <span class="v2-bar-track"><span class="v2-bar-fill" :style="{width:c.pct+'%'}"></span></span>
                  <span class="v2-bar-val">{{ M.fmtNum(c.n) }}</span>
                </div>
              </div>
            </div>
            <div class="v2-narr-block">
              <h3 class="v2-narr-block-title">时段分布</h3>
              <p class="v2-narr-block-desc">24 小时里什么时候话最多。</p>
              <div class="v2-card">
                <div class="v2-bar-row" v-for="(h,i) in hourlyRows" :key="i">
                  <span class="v2-bar-label">{{ String(h.h).padStart(2,'0') }}:00</span>
                  <span class="v2-bar-track"><span class="v2-bar-fill" :style="{width:h.pct+'%'}"></span></span>
                  <span class="v2-bar-val">{{ M.fmtNum(h.n) }}</span>
                </div>
              </div>
            </div>
          </div>

          <div class="v2-narr-block" v-if="weekdayRows.length">
            <h3 class="v2-narr-block-title">一周节奏</h3>
            <p class="v2-narr-block-desc">哪天话最多。</p>
            <div class="v2-card">
              <div class="v2-bar-row" v-for="(w,i) in weekdayRows" :key="i">
                <span class="v2-bar-label">{{ w.day }}</span>
                <span class="v2-bar-track"><span class="v2-bar-fill" :style="{width:w.pct+'%'}"></span></span>
                <span class="v2-bar-val">{{ M.fmtNum(w.total) }}</span>
              </div>
            </div>
          </div>

          <div class="v2-narr-block">
            <h3 class="v2-narr-block-title">聊得最多的会话</h3>
            <p class="v2-narr-block-desc">按消息条数排序的前 12 个。</p>
            <div class="v2-card">
              <div class="v2-bar-row" v-for="(c,i) in topContacts" :key="i">
                <span class="v2-bar-label">{{ c.name }}</span>
                <span class="v2-bar-track"><span class="v2-bar-fill" :style="{width:c.pct+'%'}"></span></span>
                <span class="v2-bar-val">{{ M.fmtNum(c.total) }}</span>
              </div>
            </div>
          </div>
        </template>

        <!-- ============ 我的画像 ============ -->
        <template v-else-if="tab==='profile'">
          <div class="v2-narr-block">
            <h3 class="v2-narr-block-title">结论速览</h3>
            <p class="v2-narr-block-desc">由数据直接统计得出，不含 AI 生成内容。</p>
            <div class="v2-card">
              <div class="v2-insight-list" v-if="insights.length">
                <div class="v2-insight-item" v-for="(it,i) in insights" :key="i">
                  <span class="v2-insight-mark"></span>
                  <span v-html="it"></span>
                </div>
              </div>
              <div v-else class="v2-empty" style="padding:24px">
                <div class="v2-empty-desc">样本不足以给出结论</div>
              </div>
            </div>
          </div>

          <div class="v2-narr-block">
            <h3 class="v2-narr-block-title">表达风格</h3>
            <p class="v2-narr-block-desc">你发消息的习惯。</p>
            <div class="v2-narr-cols">
              <div class="v2-card">
                <div class="v2-metrics" style="grid-template-columns:1fr 1fr;gap:16px">
                  <div class="v2-metric accent">
                    <div class="v2-metric-val">{{ style.avgLength }}</div>
                    <div class="v2-metric-label">平均长度（字）</div>
                  </div>
                  <div class="v2-metric">
                    <div class="v2-metric-val">{{ pct(style.emojiRate) }}</div>
                    <div class="v2-metric-label">表情使用率</div>
                  </div>
                  <div class="v2-metric">
                    <div class="v2-metric-val">{{ pct(style.questionRate) }}</div>
                    <div class="v2-metric-label">提问率</div>
                  </div>
                  <div class="v2-metric">
                    <div class="v2-metric-val">{{ M.fmtNum(style.byKind && style.byKind.text) }}</div>
                    <div class="v2-metric-label">文本条数</div>
                  </div>
                </div>
              </div>
              <div class="v2-card">
                <div class="v2-card-title" style="margin-bottom:10px">高频词</div>
                <div style="display:flex;flex-wrap:wrap;gap:6px">
                  <span v-for="k in keywords" :key="k.word" class="v2-tag">
                    {{ k.word }}<span style="color:var(--text-4)">{{ k.count }}</span>
                  </span>
                </div>
              </div>
            </div>
          </div>

          <div class="v2-narr-block">
            <h3 class="v2-narr-block-title">话题分布</h3>
            <p class="v2-narr-block-desc">按主题聚类，看你最常聊什么。</p>
            <div class="v2-card">
              <div class="v2-bar-row" v-for="(t,i) in topics" :key="i">
                <span class="v2-bar-label">{{ t.name || t.topic }}</span>
                <span class="v2-bar-track"><span class="v2-bar-fill" :style="{width:t.pct+'%'}"></span></span>
                <span class="v2-bar-val">{{ M.fmtNum(t.count) }}</span>
              </div>
            </div>
          </div>

          <div class="v2-narr-block">
            <h3 class="v2-narr-block-title">月度节奏</h3>
            <p class="v2-narr-block-desc">每月我发出 / 收到的消息量。</p>
            <div class="v2-card">
              <div class="v2-bar-row" v-for="(m,i) in monthly" :key="i">
                <span class="v2-bar-label">{{ m.month }}</span>
                <span class="v2-bar-track">
                  <span class="v2-bar-fill" :style="{width: m.pct+'%'}"></span>
                </span>
                <span class="v2-bar-val">{{ M.fmtNum(m.total) }}</span>
              </div>
            </div>
          </div>

          <div class="v2-note" style="margin-top:20px">
            分析样本 {{ M.fmtNum(meta.scannedMessages) }} 条，文本采样 {{ M.fmtNum(meta.textSampled) }} 条，
            耗时 {{ meta.cost }} 秒{{ meta.truncated ? '（已达采样上限，结果为近似值）' : '' }}。
          </div>
        </template>

        <!-- ============ 年度总结 ============ -->
        <template v-else>
          <div class="v2-narr-block">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
              <div>
                <h3 class="v2-narr-block-title" style="margin-bottom:2px">年度总结</h3>
                <p class="v2-narr-block-desc">{{ reportYear }}</p>
              </div>
              <div class="v2-seg">
                <button v-for="y in years" :key="y" :class="{active: year===y}" @click="setYear(y)">{{ y }}</button>
              </div>
            </div>
            <div class="v2-narr-lead">
              <h2 class="v2-narr-lead-title">
                {{ M.fmtNum(rs.totalMessages) }} 条消息，
                {{ M.fmtNum(rs.sentMessages) }} 条是你发的。
              </h2>
              <div class="v2-narr-metrics">
                <div class="v2-metric accent">
                  <div class="v2-metric-val">{{ M.fmtNum(rs.totalMessages) }}</div>
                  <div class="v2-metric-label">全部</div>
                </div>
                <div class="v2-metric">
                  <div class="v2-metric-val">{{ M.fmtNum(rs.sentMessages) }}</div>
                  <div class="v2-metric-label">我发出</div>
                </div>
                <div class="v2-metric">
                  <div class="v2-metric-val">{{ M.fmtNum(rs.receivedMessages) }}</div>
                  <div class="v2-metric-label">收到</div>
                </div>
                <div class="v2-metric">
                  <div class="v2-metric-val">{{ M.fmtNum(rs.activeDays) }}</div>
                  <div class="v2-metric-label">活跃天数</div>
                </div>
              </div>
            </div>
          </div>

          <div class="v2-narr-cols">
            <div class="v2-narr-block">
              <h3 class="v2-narr-block-title">最常联系</h3>
              <p class="v2-narr-block-desc">这一段时间里聊得最多的人。</p>
              <div class="v2-card">
                <div class="v2-bar-row" v-for="(c,i) in reportContacts" :key="i">
                  <span class="v2-bar-label">{{ c.name }}</span>
                  <span class="v2-bar-track"><span class="v2-bar-fill" :style="{width:c.pct+'%'}"></span></span>
                  <span class="v2-bar-val">{{ M.fmtNum(c.total) }}</span>
                </div>
              </div>
            </div>
            <div class="v2-narr-block">
              <h3 class="v2-narr-block-title">高频词</h3>
              <p class="v2-narr-block-desc">出现最多的词。</p>
              <div class="v2-card">
                <div style="display:flex;flex-wrap:wrap;gap:6px">
                  <span v-for="k in reportKeywords" :key="k.word" class="v2-tag">
                    {{ k.word }}<span style="color:var(--text-4)">{{ k.count }}</span>
                  </span>
                </div>
              </div>
            </div>
          </div>

          <div class="v2-narr-block">
            <h3 class="v2-narr-block-title">一天里的节奏</h3>
            <p class="v2-narr-block-desc">24 小时消息分布。</p>
            <div class="v2-card">
              <div class="v2-bar-row" v-for="(h,i) in reportHours" :key="i">
                <span class="v2-bar-label">{{ String(h.h).padStart(2,'0') }}:00</span>
                <span class="v2-bar-track"><span class="v2-bar-fill" :style="{width:h.pct+'%'}"></span></span>
                <span class="v2-bar-val">{{ M.fmtNum(h.n) }}</span>
              </div>
            </div>
          </div>
        </template>
      </div>
    </div>`,
    setup(props) {
      const tab = ref("overview");
      const loading = ref(false);
      const error = ref("");
      const profile = ref(null);
      const report = ref(null);
      const heat = ref(null);
      const year = ref(null);

      const ov = computed(() => (profile.value && profile.value.overview) || {});
      const meta = computed(() => (profile.value && profile.value.meta) || {});
      const style = computed(() => (profile.value && profile.value.style) || {});
      const total = computed(() => ov.value.totalMessages || 0);

      const insights = computed(() => (profile.value && profile.value.insights) || []);
      const keywords = computed(() => {
        const t = (profile.value && profile.value.topics) || {};
        return (t.keywords || []).slice(0, 24);
      });
      const topics = computed(() => {
        const t = (profile.value && profile.value.topics) || {};
        // 后端 byTopic 项形如 { topic, hits, ratio }
        const list = t.byTopic || [];
        const max = list.reduce((a, x) => Math.max(a, x.hits || 0), 0) || 1;
        return list.map((x) => Object.assign({}, x, {
          name: x.topic || x.name,
          count: x.hits || x.count || 0,
          pct: Math.round((x.hits || 0) / max * 100),
        }));
      });
      const monthly = computed(() => {
        const list = (profile.value && profile.value.monthly) || [];
        const max = list.reduce((a, x) => Math.max(a, x.total || 0), 0) || 1;
        return list.map((x) => Object.assign({}, x, { pct: Math.round((x.total || 0) / max * 100) }));
      });

      const kindRows = computed(() => {
        const bk = style.value.byKind || {};
        const names = { text: "文本", image: "图片", file: "文件", voice: "语音", video: "视频", other: "其他", system: "系统" };
        const list = Object.entries(bk).map(([k, n]) => ({ name: names[k] || k, n }));
        const total = list.reduce((a, x) => a + x.n, 0) || 1;
        return list.sort((a, b) => b.n - a.n).map((x) => Object.assign(x, { pct: Math.round(x.n / total * 100) }));
      });

      const hourlyRows = computed(() => {
        const rh = (profile.value && profile.value.rhythm) || {};
        const list = rh.hourly || [];
        // 后端 hourly 项形如 { hour, total, mine }
        const max = list.reduce((a, x) => Math.max(a, x.total || 0), 0) || 1;
        return list.map((x) => ({ h: x.hour, n: x.total || 0 }))
          .map((x) => Object.assign(x, { pct: Math.round(x.n / max * 100) }));
      });

      const topContacts = computed(() => {
        const list = (profile.value && profile.value.social) || [];
        const max = list.reduce((a, x) => Math.max(a, x.total || 0), 0) || 1;
        return list.slice(0, 12).map((x) => Object.assign({}, x, { pct: Math.round((x.total || 0) / max * 100) }));
      });

      const calendar = computed(() => {
        // 日粒度活跃度来自 /api/stats/time?by=date（profile 里没有这层粒度）
        const dist = heat.value || {};
        const entries = Object.entries(dist).sort((a, b) => a[0].localeCompare(b[0]));
        if (!entries.length) return [];
        const counts = entries.map(([, n]) => n);
        const max = Math.max.apply(null, counts) || 1;
        // 后端只给出「有消息的日期」，中间可能缺整段（如换号/未登录的空档）。
        // 若直接首尾相接，空档之后的星期对齐全错位。→ 先补成连续日期轴。
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
            // 无消息的日子用 level 0（空格子），与「有消息但很少」区分开
            level: n === 0 ? 0 : Math.min(5, Math.ceil(n / max * 5)),
          });
        }
        // 按周对齐：首行前面补空格，让每列对应同一星期几；末行补齐到整周
        const lead = first.getDay();   // 0=周日
        const cells = [];
        for (let i = 0; i < lead; i++) cells.push(null);
        days.forEach((c) => cells.push(c));
        const rem = cells.length % 7;
        if (rem) for (let i = 0; i < 7 - rem; i++) cells.push(null);
        return cells;
      });

      // 有记录的天数（calendar 里混了首尾补位的空格子，不能直接用 length）
      const heatDays = computed(() => {
        const dist = heat.value || {};
        return Object.values(dist).filter((n) => Number(n) > 0).length;
      });

      // 日历网格：每列 7 天（按周对齐），列宽 11px + 间距 3px
      const HEAT_COL_W = 14;
      const heatCols = computed(() => Math.ceil(calendar.value.length / 7));
      const heatWidth = computed(() => heatCols.value * HEAT_COL_W - 3);

      // 月份标签：在每个月的第一天所在列上方标注，间距过近的合并掉
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
          const col = Math.floor(i / 7);
          const left = col * HEAT_COL_W;
          // 与上一个标签间距不足 28px 就跳过，避免文字叠字
          if (out.length && left - out[out.length - 1].left < 28) return;
          out.push({
            key: key,
            left: left,
            label: (d.getMonth() + 1) + "月",
          });
        });
        return out;
      });

      const weekdayRows = computed(() => {
        const rh = (profile.value && profile.value.rhythm) || {};
        const list = rh.weekday || [];
        const max = list.reduce((a, x) => Math.max(a, x.total || 0), 0) || 1;
        return list.map((x) => Object.assign({}, x, { pct: Math.round((x.total || 0) / max * 100) }));
      });

      const rs = computed(() => (report.value && report.value.summary) || {});
      const reportContacts = computed(() => {
        // 后端 topContacts 项形如 { wxid, displayName, count }
        const list = rs.value.topContacts || [];
        const max = list.reduce((a, x) => Math.max(a, x.count || 0), 0) || 1;
        return list.map((x) => Object.assign({}, x, {
          name: x.displayName || x.name || x.wxid,
          total: x.count || x.total || 0,
          pct: Math.round((x.count || 0) / max * 100),
        }));
      });
      const reportKeywords = computed(() => (rs.value.topKeywords || []).slice(0, 30));
      const reportHours = computed(() => {
        const hd = rs.value.hourlyDistribution || {};
        const list = Object.entries(hd).map(([h, n]) => ({ h: Number(h), n }));
        const max = list.reduce((a, x) => Math.max(a, x.n), 0) || 1;
        return list.map((x) => Object.assign(x, { pct: Math.round(x.n / max * 100) }));
      });

      const years = computed(() => {
        const y = new Date().getFullYear();
        return [y, y - 1, y - 2];
      });
      const reportYear = computed(() => (report.value && report.value.year) || "全部年份");

      function pct(x) {
        if (x === null || x === undefined) return "—";
        const n = Number(x);
        if (n <= 1) return (n * 100).toFixed(1);
        return n.toFixed(1);
      }

      function heatColor(level) {
        const map = ["var(--heat-0)", "var(--heat-1)", "var(--heat-2)", "var(--heat-3)", "var(--heat-4)", "var(--heat-5)"];
        return map[level] || map[0];
      }

      async function load() {
        loading.value = true;
        error.value = "";
        try {
          // 三个接口并行。/api/profile 首次要扫全库（~5s），其余走缓存（~0s）。
          // 不用 Promise.all —— 一个失败会把另外两个的结果一起丢掉。
          const [p, r, h] = await Promise.allSettled([
            M.api.get("/api/profile"),
            M.api.get("/api/report", year.value ? { year: year.value } : {}),
            M.api.get("/api/stats/time", { by: "date" }),
          ]);
          if (p.status === "fulfilled" && p.value) profile.value = p.value;
          if (r.status === "fulfilled" && r.value) report.value = r.value;
          if (h.status === "fulfilled" && h.value) heat.value = h.value.distribution || {};

          if (!profile.value && !report.value) {
            const why = p.status === "rejected" ? p.reason : (r.status === "rejected" ? r.reason : null);
            throw new Error(why ? String(why.message || why) : "后端未返回数据");
          }
          if (!year.value && report.value && report.value.year) year.value = report.value.year;
        } catch (e) {
          error.value = "加载失败：" + e.message;
        } finally {
          loading.value = false;
        }
      }

      async function reload() { await load(); }

      async function setYear(y) {
        year.value = y;
        try {
          report.value = await M.api.get("/api/report", { year: y });
        } catch (e) {
          M.toast.err("加载年度数据失败：" + e.message);
        }
      }

      onMounted(async () => {
        // 路由带 tab：insight.profile / insight.report
        if (props.route && props.route.includes(".")) {
          const t = props.route.split(".")[1];
          if (TABS.some((x) => x.id === t)) tab.value = t;
        }
        await load();
      });

      return {
        tabs: TABS, tab, loading, error, ov, meta, style, total,
        insights, keywords, topics, monthly, kindRows, hourlyRows, weekdayRows, topContacts, calendar,
        heatDays, heatWidth, heatMonths,
        rs, reportContacts, reportKeywords, reportHours, years, year, reportYear,
        pct, heatColor, reload, setYear, M,
      };
    },
  };

  global.MosuV2 = global.MosuV2 || {};
  global.MosuV2.Insight = Insight;
})(window);
