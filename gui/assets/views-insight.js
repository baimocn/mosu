/* ==========================================================================
   页面 · 洞察（数据统计 / 画像 / 年报）+ 通用结构化渲染器
   后端字段是统计口径的原始输出，直接倾倒会变成「给人机看的 JSON」。
   这里的原则：已知字段 → 专属布局 + 口径注释；未知字段 → 自适应兜底，
   任何结构都不会白屏。所有数值在展示层转成人类单位（%、分钟）。
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

  /** 把后端的原始数值转成人类单位：占比→百分比，秒→分钟，大数→千分位 */
  function humanVal(v, key) {
    if (typeof v !== "number") return v;
    const k = key || "";
    if (/(^|_)(share|ratio|rate)$/i.test(k) || /^(myShare|emojiRate|questionRate)$/.test(k)) {
      return Math.round(v * 1000) / 10 + "%";
    }
    if (k === "avgLength") return v + " 字";
    if (/Seconds$/.test(k)) {
      if (v >= 3600) return (v / 3600).toFixed(1) + " 小时";
      if (v >= 60) return Math.round(v / 60) + " 分钟";
      return v + " 秒";
    }
    return M.fmtNum(v);
  }

  /* ------------------------------------------------- 字段名 → 中文标签 */
  const FIELD_LABELS = {
    // 规模
    totalMessages: "总消息", myMessages: "我发出", theirMessages: "对方发出",
    sentMessages: "我发出", receivedMessages: "对方发出",
    textMessages: "文本", mediaMessages: "图片视频等媒体",
    activeDays: "活跃天数", spanDays: "跨越天数",
    firstText: "首条时间", lastText: "最近时间",
    firstMessageTime: "首条消息", lastMessageTime: "最近消息",
    firstMessage: "首条消息", lastMessage: "最近消息",
    // 元信息
    scannedMessages: "扫描消息", textSampled: "抽样文本", cost: "用时",
    // 节奏
    peakHour: "最活跃小时", peakWeekday: "最活跃星期",
    quietest4hStart: "最安静时段", nightMessages: "凌晨消息（0-5 点）",
    morningMessages: "清晨消息（6-9 点）", hourly: "逐小时",
    hourlyMine: "我（逐小时）", hourlyTheirs: "对方（逐小时）",
    weekday: "按星期", monthly: "按月", byKind: "按消息类型",
    // 风格
    avgLength: "平均消息长度", emojiRate: "表情占比", questionRate: "带问号占比",
    // 互动
    initiatedByMe: "我开启的对话", initiatedByThem: "对方开启的对话",
    myAvgReplySeconds: "我的平均回复", theirAvgReplySeconds: "对方的平均回复",
    // 话题
    byTopic: "话题", keywords: "高频词", keywordsMine: "我的常用词",
    keywordsTheirs: "对方的常用词", bigrams: "高频二字组合",
    mine: "我", theirs: "对方", topic: "话题", hits: "命中消息", word: "词",
    // 情绪
    avg: "平均分", positive: "偏正面条数", negative: "偏负面条数",
    neutral: "中性条数", positiveRate: "正面占比", curve: "逐月情绪曲线",
    // 信号
    laughPer100: "每百条「哈」字数", lengthTrend: "消息长度走势",
    mediaShare: "非文本占比", voiceMine: "我的语音", voiceTheirs: "对方的语音",
    imageMine: "我的图片", imageTheirs: "对方的图片",
    // 时刻
    time: "时间", who: "说话人", content: "内容", sample: "内容样本",
    day: "日期", busiestDay: "最热闹的一天", firstVoice: "第一条语音",
    firstImage: "第一张图片", firstVideo: "第一个视频",
    // 年报
    topContacts: "最常联系", topKeywords: "高频词",
    hourlyDistribution: "时段分布",
    // 联系人 / 群
    activeMembers: "活跃成员", topMembers: "成员排行",
    messageCount: "消息数", count: "数量", total: "合计", ratio: "占比",
    share: "占比", isSelf: "是我", displayName: "名称", senderId: "微信 ID",
    sessions: "会话", messages: "消息", name: "名称", nickname: "昵称",
    remark: "备注", wxid: "微信 ID", traits: "特征", summary: "摘要",
    sentiment: "情感", score: "分值", firstTime: "首次", lastTime: "最近",
    year: "年份", replyLatency: "回复时延", style: "风格",
    myShare: "我发出的占比", avgLength: "平均消息长度",
    // 消息类型（后端 byKind 的键是英文小写）
    text: "文本", image: "图片", voice: "语音", video: "视频",
    file: "文件", emoji: "表情", system: "系统", link: "链接", other: "其他",
  };
  /** 渲染层信号，不该出现在任何卡片里：
      meta.truncated 已在页首脚注说明（见 Insight.metaLine），
      overview.truncated 与它同义，重复展示只是噪音；
      milestones.firstMessage.sampledOnly 由 JsonView.isSampledFirst
      翻译成「样本内最早」徽标 + tooltip。
      注意用显式集合而不是「标签置空」——label() 里的 `|| k` 会把空串回退成原键。 */
  const FIELD_HIDDEN = new Set(["truncated", "sampledOnly"]);
  function label(k) { return FIELD_LABELS[k] || k; }
  function isHiddenField(k) { return FIELD_HIDDEN.has(k); }

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
            <template v-if="isPrimitive(v)">{{ v }}</template>
            <template v-else-if="isObj(v) && v.word !== undefined">
              {{ v.word }} <span class="muted tiny">× {{ M.fmtNum(v.count) }}</span>
            </template>
            <template v-else-if="isObj(v) && v.topic !== undefined">
              {{ v.topic }} <span class="muted tiny">
                {{ M.fmtNum(v.hits) }} 条 · 占{{ pctOf(v.ratio) }}</span>
            </template>
            <json-view v-else :data="v" :depth="depth+1" />
          </div>
          <div v-if="!data.length" class="muted tiny">（这一项没有数据）</div>
        </template>
        <dl v-else-if="dataIsObject" class="kv">
          <template v-for="(v,k) in visible" :key="k">
            <dt :title="isSampledFirst(v) ? sampledTip(k) : ''">{{ fieldLabel(k, v) }}</dt>
            <dd>
              <!-- 截断样本里的「首条消息」不是会话真实首条，必须换个说法。
                   走通用 KV 会渲染成「样本内最早: true」，既噪音又没传达意思。 -->
              <div v-if="isSampledFirst(v)" class="sampled-first"
                   :title="sampledTip(k)">
                <span class="badge badge-warn">样本内最早</span>
                <span class="muted tiny">非真实首条</span>
                <div style="margin-top:4px">{{ bodyOf(k, v) }}</div>
                <div class="tiny muted" style="margin-top:2px">
                  {{ v.who || '—' }} · {{ v.time || '—' }}
                </div>
              </div>
              <template v-else-if="isPrimitive(v)">{{ fmtVal(v, k) }}</template>
              <json-view v-else :data="v" :depth="depth+1" />
            </dd>
          </template>
        </dl>
        <div v-else>{{ data }}</div>
      </div>`,
    computed: {
      // 同上：methods 只绑定函数，对象要放 computed 模板才读得到
      M() { return M; },
      isList() { return Array.isArray(this.data); },
      /** 「当前 data 是不是一个对象」——注意别再叫 isObj：
       *  methods 里还有一个判任意值的 isObj(v)，同名会被 method 覆盖，
       *  这里就会变成拿到函数对象（恒真），整页渲染随即崩掉。 */
      dataIsObject() { return this.data && typeof this.data === "object" && !Array.isArray(this.data); },
      /** 空值字段直接不渲染，不占版面 */
      visible() {
        const out = {};
        for (const [k, v] of Object.entries(this.data || {})) {
          if (v === null || v === undefined || v === "") continue;
          if (Array.isArray(v) && !v.length) continue;
          if (v && typeof v === "object" && !Array.isArray(v) && !Object.keys(v).length) continue;
          /* sampledOnly 是给渲染层的信号，不是给人看的内容：
             它已被 isSampledFirst 翻译成「样本内最早」徽标 + tooltip。
             truncated 同理：页首脚注已说明样本上限，卡片里不必重复。 */
          if (isHiddenField(k)) continue;
          out[k] = v;
        }
        return out;
      },
    },
    methods: {
      label,
      isPrimitive(v) {
        return v === null || v === undefined
          || ["string", "number", "boolean"].includes(typeof v);
      },
      isObj(v) { return v && typeof v === "object" && !Array.isArray(v); },
      /**
       * 后端在截断时给 milestones.firstMessage 打 sampledOnly：
       * 那条只是**样本内**最早的一条，不是会话真实首条。
       * 不换说法的话，用户会把它读成「他们第一次聊天」——那是假的。
       */
      isSampledFirst(v) {
        return !!v && typeof v === "object" && !Array.isArray(v) && v.sampledOnly === true;
      },
      /**
       * 图片 / 视频 / 语音**本来就没有正文**，不是内容丢了。
       * 统一显示「（无正文）」会让这屏看起来像出了故障——实测 14.9 万条的群，
       * 四条里程碑全是「无正文」，用户第一反应是「这功能坏了」。
       * 按字段语义给不同的说法，纯媒体项只报类型与时间即可。
       */
      bodyOf(k, v) {
        const c = v.content;
        if (c === null || c === undefined) {
          return { firstImage: "（图片消息）", firstVideo: "（视频消息）",
                   firstVoice: "（语音消息）" }[k] || "（该条无文字）";
        }
        if (String(c).trim() === "") return "（该条无文字）";
        return c;
      },
      /** tooltip 也要分项：语音/图片的「第一次」与「第一次聊天」不是一回事 */
      sampledTip(k) {
        const what = { firstMessage: "第一次聊天", firstVoice: "第一条语音",
                       firstImage: "第一张图片", firstVideo: "第一个视频" }[k] || "这一项";
        return `本次分析已达样本上限，这里显示的是样本范围内的${what}，`
             + "不是这个会话真实的第一次。";
      },
      /**
       * 截断样本里 `<dt>` 也要降级措辞。
       *
       * 为什么不能改 FIELD_LABELS：那张表是全局静态的，一改就连
       * **未截断**的正常数据也变成「样本内最早语音」——那才是真的假数据
       * （把真实的第一条语音说成不是第一条）。降级只能按当行数据决定。
       *
       * 为什么不能只靠徽标：徽标在 `<dd>` 里，而视线先扫 `<dt>`。
       * 「第一条语音」+ 旁边一个「样本内最早」仍会被读成
       * 「他们第一次发语音是 7 月 27 日」。两处都降级才留得住。
       */
      fieldLabel(k, v) {
        if (!this.isSampledFirst(v)) return this.label(k);
        return { firstMessage: "样本内最早消息", firstVoice: "样本内最早语音",
                 firstImage: "样本内最早图片", firstVideo: "样本内最早视频" }[k]
            || this.label(k);
      },
      fmtVal(v, k) { return humanVal(v, k); },
      pctOf(r) { return typeof r === "number" ? Math.round(r * 1000) / 10 + "%" : "—"; },
    },
  };

  /* ---------------------------------------------------------------- 心理学维度 */
  /* 为什么不用 JsonView：这一块的核心不是「有哪些字段」，而是
     「每条判断的证据是什么、置信度多少、反例是什么」。
     倒成 JSON 用户只能看到一行行数字，反而更容易把推测当成结论。 */
  const PsychView = {
    name: "PsychView",
    props: { data: { type: Object, default: null } },
    template: `
    <div>
      <div v-if="!dims.length" class="muted tiny">
        {{ (data && data.method && data.method.note) || '样本不足，未生成心理学维度。' }}
      </div>
      <template v-else>
        <div v-for="d in dims" :key="d.key" class="psych-dim">
          <div class="psych-head">
            <span class="psych-label">{{ d.label }}</span>
            <span class="psych-conf" :class="'conf-' + d.confidence">{{ confText(d.confidence) }}</span>
            <span class="spacer" style="flex:1"></span>
            <span class="tiny muted">样本 {{ fmtNum(d.confidenceBasis) }}</span>
          </div>
          <div v-for="(e,i) in d.evidence" :key="i" class="psych-ev">
            <div class="psych-ev-row">
              <span class="psych-ev-beh">{{ e.behavior }}</span>
              <span class="psych-ev-val">{{ fmtVal(e) }}</span>
              <span class="tiny muted">样本 {{ fmtNum(e.basis) }}</span>
            </div>
            <div v-if="e.caveat" class="psych-caveat">
              <span class="psych-caveat-tag">反例可能</span>{{ e.caveat }}
            </div>
          </div>
        </div>
        <div v-if="data.disclaimer" class="psych-note">{{ data.disclaimer }}</div>
      </template>
    </div>`,
    computed: {
      dims() { return (this.data && this.data.dimensions) || []; },
    },
    methods: {
      fmtNum: (v) => M.fmtNum(v),
      confText(c) {
        return { high: "证据较强", medium: "证据中等", low: "证据较弱",
                 insufficient: "证据不足" }[c] || c;
      },
      fmtVal(e) {
        const v = e.value;
        const beh = e.behavior || "";
        /* 不能只判「值 ≤1 就当百分比」：「回复耗时比 1.47」是倍数，
           按比例显示会变成 147%，直接读错。 */
        if (beh.includes("之比") || (beh.includes("耗时") && beh.includes("/"))
            || beh.includes("倍")) {
          return String(v);
        }
        if (typeof v === "number" && v >= 0 && v <= 1) {
          /* 用原始值判阈值：先 Math.round 会让 0.06% 变成 0.1%，夸大十倍 */
          if (v * 100 < 0.1) return "<0.1%";
          return String(Math.round(v * 1000) / 10) + "%";
        }
        return String(v);
      },
    },
  };

  /* ---------------------------------------------------------------- 数据统计 */
  const Stats = {
    components: { JsonView },
    template: `
    <div class="page">
      <div v-if="loading" class="grid grid-4">
        <div v-for="i in 4" class="skeleton" :key="i" style="height:96px"></div>
      </div>
      <template v-else-if="isEmpty">
        <div class="empty">
          <div class="empty-icon">📊</div>
          <div class="empty-title">还没有可统计的消息</div>
          <div class="empty-desc">
            数据库里一条消息都没有。先在「设置」里配好密钥或确认数据目录指向了正确的
            微信聊天记录文件夹，配置生效后回到这一页刷新。
          </div>
        </div>
      </template>
      <template v-else-if="g">
        <div class="grid grid-4">
          <div class="tile rise">
            <div class="tile-head">消息总数</div>
            <div class="tile-value">{{ fmtNum(g.totalMessages) }}</div>
            <div class="tile-foot">分布在 {{ fmtNum(g.totalSessions) }} 个会话</div>
            <div class="tile-foot" v-if="g.deduped === false">
              按数据库原始行统计，跨分片重复行未去重
            </div>
          </div>
          <div class="tile rise">
            <div class="tile-head">联系人</div>
            <div class="tile-value">{{ fmtNum(g.totalContacts) }}</div>
            <div class="tile-foot">通讯录全部条目，含群聊与公众号</div>
          </div>
          <div class="tile rise">
            <div class="tile-head">最活跃的一天</div>
            <div class="tile-value" style="font-size:20px">{{ peakDayText }}</div>
            <div class="tile-foot">{{ peakDayCount }}</div>
          </div>
          <div class="tile rise">
            <div class="tile-head">记录时间跨度</div>
            <div class="tile-value" style="font-size:15px;line-height:1.35">
              {{ (g.timeRange && g.timeRange.firstText) || '—' }}
              <span class="muted">→</span>
              {{ (g.timeRange && g.timeRange.lastText) || '—' }}
            </div>
            <div class="tile-foot">{{ spanDays ? '共 ' + fmtNum(spanDays) + ' 天' : '' }}</div>
          </div>
        </div>

        <div class="card" style="margin-top:16px">
          <div class="card-title">活跃度日历</div>
          <div class="card-sub">最近一年每天的消息条数，颜色越深聊得越多。悬停可看具体数字。</div>
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
              <span>有消息 {{ fmtNum(activeDays) }} 天 · 最长连续 {{ fmtNum(maxStreak) }} 天</span>
            </div>
          </template>
          <div v-else class="muted tiny">最近一年没有消息记录。</div>
        </div>

        <div class="grid grid-2" style="margin-top:16px">
          <div class="card">
            <div class="card-title">消息构成</div>
            <div class="card-sub">文本之外的消息按类型列出，可以用来估计聊天里图片、语音的分量。</div>
            <div v-for="(v,k) in g.byType" :key="k" class="bar-row">
              <div class="bar-label">{{ typeName(k) }}</div>
              <div class="bar-track">
                <div class="bar-fill" :style="{width: pct(v, maxType)}"></div>
              </div>
              <div class="bar-value">{{ fmtNum(v) }}</div>
            </div>
          </div>

          <div class="card">
            <div class="card-title">聊得最多的会话</div>
            <div class="card-sub">按消息条数取前 12 个，群聊也算在内。</div>
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
            什么时间在聊
            <div class="spacer"></div>
            <button class="chip" :class="{active: by==='hour'}" @click="switchBy('hour')">按小时</button>
            <button class="chip" :class="{active: by==='weekday'}" @click="switchBy('weekday')">按星期</button>
            <button class="chip" :class="{active: by==='month'}" @click="switchBy('month')">按月</button>
          </div>
          <div class="card-sub">消息条数在 24 小时 / 星期 / 月份上的分布，按数据库记录时间统计。</div>
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
          <div class="card-sub">对全部文本消息分词计数，字号代表出现次数。全库较大时需要一两分钟，在后台跑。</div>
          <div v-if="kwRunning" class="progress progress-bar indeterminate"
               style="margin-bottom:8px"></div>
          <div v-if="keywords.length" style="display:flex;flex-wrap:wrap;gap:8px">
            <span v-for="k in keywords" :key="k.word" class="chip"
                  :style="{fontSize: kwSize(k.count)}">
              {{ k.word }} · {{ k.count }}
            </span>
          </div>
          <div v-else-if="!kwRunning" class="muted tiny">点「开始分析」生成。</div>
        </div>
      </template>
      <div v-else class="empty">
        <div class="empty-icon">📊</div>
        <div class="empty-title">还没有可统计的数据</div>
        <div class="empty-desc">{{ err || '先在「设置」里配好密钥和数据目录，再回到这一页。' }}</div>
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
      /** 空库：一屏 0 比报错更糟，明确告诉用户下一步做什么 */
      isEmpty() { return !!this.g && !(this.g.totalMessages > 0); },
      maxType() { return Math.max(1, ...Object.values(this.g ? this.g.byType : { x: 1 })); },
      maxContact() { return Math.max(1, ...this.contacts.map((c) => c.messageCount)); },
      maxTime() { return Math.max(1, ...(this.time ? this.time.items.map((i) => i.count) : [1])); },
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
      peakDayCount() { return this.peakDay.count ? `${M.fmtNum(this.peakDay.count)} 条消息` : "暂无数据"; },
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
                 file: "文件", emoji: "表情", system: "系统", link: "链接",
                 other: "其他" }[k] || k;
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
      /** 逐日聚合要扫全库，单独加载，不拖慢首屏；空库时没有可聚合的东西 */
      async loadHeat() {
        if (this.isEmpty) { this.heatLoading = false; return; }
        this.heatLoading = true;
        try { this.heat = await M.api.get("/api/stats/time", { by: "date" }); }
        catch (e) { this.heat = null; }
        finally { this.heatLoading = false; }
      },
      async runKeywords() {
        this.kwRunning = true;
        try {
          const d = await M.api.post("/api/stats/keywords", { top: 60 });
          const tid = d.taskId || d.id || (d.task && d.task.id);
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

  /* ---------------------------------------------------------------- 年报 */
  /* 年报后端只有一个 summary 大对象，值得专属排版而不是 KV 倾倒 */
  const ReportView = {
    name: "ReportView",
    props: { data: { type: Object, required: true } },
    template: `
    <div>
      <div class="tiny muted" v-if="metaLine" style="margin-bottom:12px">{{ metaLine }}</div>
      <div class="grid grid-4">
        <div class="tile rise">
          <div class="tile-head">总消息</div>
          <div class="tile-value">{{ fmtNum(s.totalMessages) }}</div>
          <div class="tile-foot">{{ title }}</div>
        </div>
        <div class="tile rise">
          <div class="tile-head">我发出</div>
          <div class="tile-value">{{ fmtNum(s.sentMessages) }}</div>
          <div class="tile-foot">占 {{ shareSent }}</div>
        </div>
        <div class="tile rise">
          <div class="tile-head">对方发出</div>
          <div class="tile-value">{{ fmtNum(s.receivedMessages) }}</div>
          <div class="tile-foot">占 {{ shareRecv }}</div>
        </div>
        <div class="tile rise">
          <div class="tile-head">有消息的天数</div>
          <div class="tile-value">{{ fmtNum(s.activeDays) }}</div>
          <div class="tile-foot">首条 {{ firstText }} · 末条 {{ lastText }}</div>
        </div>
      </div>

      <div class="grid grid-2" style="margin-top:16px">
        <div class="card">
          <div class="card-title">最常联系</div>
          <div class="card-sub">这一年消息条数最多的会话。</div>
          <div v-for="c in s.topContacts.slice(0,10)" :key="c.wxid" class="bar-row">
            <div class="bar-label" :title="c.displayName">{{ c.displayName }}</div>
            <div class="bar-track">
              <div class="bar-fill" :style="{width: pct(c.count, maxContact)}"></div>
            </div>
            <div class="bar-value">{{ fmtNum(c.count) }}</div>
          </div>
          <div v-if="!(s.topContacts || []).length" class="muted tiny">（无记录）</div>
        </div>
        <div class="card">
          <div class="card-title">时段分布</div>
          <div class="card-sub">24 小时里什么点最热闹，按小时合计全部消息。</div>
          <div v-for="h in hourRows" :key="h.h" class="bar-row">
            <div class="bar-label">{{ h.h }} 点</div>
            <div class="bar-track">
              <div class="bar-fill" :style="{width: pct(h.count, maxHour)}"></div>
            </div>
            <div class="bar-value">{{ fmtNum(h.count) }}</div>
          </div>
        </div>
      </div>

      <div class="card" style="margin-top:16px">
        <div class="card-title">这一年常说什么</div>
        <div class="card-sub">全部文本消息分词后出现最多的词。</div>
        <div v-if="(s.topKeywords || []).length" style="display:flex;flex-wrap:wrap;gap:8px">
          <span v-for="k in s.topKeywords.slice(0,40)" :key="k.word" class="chip"
                :style="{fontSize: kwSize(k.count)}">{{ k.word }} · {{ fmtNum(k.count) }}</span>
        </div>
        <div v-else class="muted tiny">（无记录）</div>
      </div>
    </div>`,
    computed: {
      s() { return this.data.summary || {}; },
      title() {
        return this.data.year ? `${this.data.year} 年` : "全部年份合计";
      },
      shareSent() {
        const t = Math.max(1, this.s.totalMessages || 0);
        return Math.round((this.s.sentMessages || 0) / t * 1000) / 10 + "%";
      },
      shareRecv() {
        const t = Math.max(1, this.s.totalMessages || 0);
        return Math.round((this.s.receivedMessages || 0) / t * 1000) / 10 + "%";
      },
      firstText() { return this.tsShort(this.s.firstMessageTime); },
      lastText() { return this.tsShort(this.s.lastMessageTime); },
      hourRows() {
        const dist = this.s.hourlyDistribution || {};
        return Object.keys(dist).sort()
          .map((h) => ({ h: parseInt(h, 10), count: Number(dist[h]) || 0 }));
      },
      maxHour() { return Math.max(1, ...this.hourRows.map((r) => r.count)); },
      maxContact() {
        return Math.max(1, ...(this.s.topContacts || []).map((c) => c.count));
      },
      metaLine() {
        const m = this.data.meta;
        if (!m) return "";
        const bits = [];
        if (m.scannedMessages) bits.push(`扫描 ${M.fmtNum(m.scannedMessages)} 条消息`);
        if (m.cost != null) bits.push(`用时 ${m.cost} 秒`);
        // 命中缓存时明确标注：否则用户会疑惑「上次 5 秒，这次怎么秒开」
        if (m.cached) bits.push("结果来自缓存");
        return bits.length ? bits.join(" · ") + " · 全部本机计算" : "";
      },
    },
    methods: {
      fmtNum: M.fmtNum,
      pct(v, max) { return Math.max(2, (v / Math.max(1, max)) * 100) + "%"; },
      kwSize(c) {
        const mx = Math.max(1, ...(this.s.topKeywords || []).map((k) => k.count));
        return (12 + Math.round((c / mx) * 8)) + "px";
      },
      tsShort(ts) {
        if (!ts) return "—";
        const d = new Date(ts * 1000);
        const pad = (n) => (n < 10 ? "0" + n : n);
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
      },
    },
  };

  /* ---------------------------------------------------------------- 画像 */
  /* 已知字段的呈现顺序与口径注释。没列出的键走「补充信息」兜底。 */
  const PROFILE_SECTIONS = {
    self: [
      ["insights", "结论速览", "由本页各项统计按固定规则归纳，非 AI 生成，可复现。"],
      ["overview", "总览"],
      ["style", "表达风格"],
      ["rhythm", "作息节奏"],
      ["topics", "话题与词汇"],
      ["social", "往来对象", "按你发出的消息条数排序，占比指你在该会话里的发言份额。"],
      ["monthly", "月度走势"],
    ],
    contact: [
      ["overview", "总览"],
      ["interaction", "互动质量",
        "「开启对话」指间隔超过 6 小时的第一条消息由谁发出；回复时长按相邻一来回估算，间隔超过 12 小时的不计入。"],
      ["signals", "聊天信号", "由消息文本与类型统计得出，反映聊天形式的变化，不评判任何一方。"],
      ["sentiment", "情绪", "基于中文情感词典逐条打分（-1 到 1），得分不明显时记为中性。"],
      ["topics", "话题与词汇", "两边分开统计，可以直接对比彼此聊的内容。"],
      ["timeline", "关键节点", "取消息量最大的三个月，各配当月第一条消息，用来回忆当时在做什么。"],
      ["milestones", "重要时刻"],
      ["rhythm", "时段分布"],
      ["psych", "心理学维度",
        "从聊天行为反推的倾向，不是心理诊断。每条判断都标了证据、样本量和反例可能；"
        + "证据不足时明确写「证据不足」，不给结论。"],
    ],
  };
  /** 这些键不进卡片：meta 进页首脚注，scope/session 已在页首体现。
      注意：truncated 契约在 meta.truncated（不是 summary.truncated），
      后端不返回 summary 键，所以这里无需隐藏任何东西。 */
  const PROFILE_HIDDEN = new Set(["meta", "scope", "session"]);

  const Insight = {
    components: { JsonView, ReportView, PsychView },
    props: { kind: { type: String, default: "profile" } },   // profile | report
    template: `
    <div class="page">
      <div class="row" style="margin-bottom:12px">
        <button class="chip" :class="{active: mode==='self'}" @click="setMode('self')"
                v-if="kind==='profile'">我的画像</button>
        <button class="chip" :class="{active: mode==='contact'}" @click="setMode('contact')"
                v-if="kind==='profile'">联系人画像</button>
        <select class="select" style="width:280px" v-if="mode==='contact'"
                v-model="picked" @change="load">
          <option value="">选择一个会话…</option>
          <option v-for="s in sessions" :key="s.sessionId" :value="s.sessionId">
            {{ s.displayName }}（{{ sessionTypeName(s.type) }}）
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
      <!-- 首次生成要跑分词（全库约 3-6 秒），只给灰块用户会以为卡死了。
           明确说出「在算什么」和「大概多久」，等待才可接受。 -->
      <div v-if="loading" class="tiny muted" style="margin-top:10px;text-align:center">
        <span class="spinner" style="margin-right:6px"></span>
        {{ loadingHint }}
      </div>
      <div v-else-if="err" class="empty">
        <div class="empty-icon">🧭</div>
        <div class="empty-title">生成失败</div>
        <div class="empty-desc">{{ err }}</div>
      </div>
      <template v-else-if="data && kind==='report'">
        <report-view :data="data" />
      </template>
      <template v-else-if="data">
        <div class="tiny muted" v-if="metaLine" style="margin-bottom:12px">{{ metaLine }}</div>
        <div class="notice" v-if="isGroupContact" style="margin-bottom:12px">
          <b>群聊口径说明</b>
          <div class="tiny" style="margin-top:4px;opacity:.85">
            群聊没有明确的「对方」。本页的互动与情绪类指标按「我 vs 群」二分计算，
            即「我发出的」对「群内其他人发出的合计」，不区分具体是哪位群成员。
          </div>
        </div>
        <div v-for="sec in sections" :key="sec.key" class="card" style="margin-bottom:16px">
          <div class="card-title">{{ sec.title }}</div>
          <div class="card-sub" v-if="sec.note">{{ sec.note }}</div>
          <div v-if="sec.key==='insights'" style="display:flex;flex-direction:column;gap:8px">
            <div v-for="(s,i) in data.insights" :key="i" style="display:flex;gap:8px">
              <span style="color:var(--brand)">›</span><span>{{ s }}</span>
            </div>
          </div>
          <psych-view v-else-if="sec.key==='psych'" :data="data.psych" />
          <json-view v-else :data="data[sec.key]" />
        </div>
        <div v-if="restKeys.length" class="card">
          <json-view :data="restData" title="补充信息" />
        </div>
      </template>
      <div v-else-if="kind==='profile' && mode==='contact'" class="empty">
        <div class="empty-icon">🧭</div>
        <div class="empty-title">选择一位联系人</div>
        <div class="empty-desc">
          生成你与 TA 的往来画像：消息量与占比、谁更常开启话题、回复快慢、
          高频词对比、情绪走向和关键节点。全部在本机计算，结果顶部会标明扫描了多少条消息。
        </div>
      </div>
      <div v-else class="empty">
        <div class="empty-icon">🧭</div>
        <div class="empty-title">{{ kind==='profile' ? '正在生成你的画像' : '生成你的微信年报' }}</div>
        <div class="empty-desc">
          {{ kind==='profile'
            ? '基于全部聊天记录归纳你的表达风格、作息节奏与常聊话题。'
            : '选定年份后生成：这一年聊了多少、和谁聊得最多、常说什么、什么时段最活跃。全部在本机完成，不上传任何数据。' }}
        </div>
      </div>
    </div>`,
    data() {
      return {
        mode: "self", picked: "", year: "", years: [], sessions: [],
        data: null, loading: false, err: "",
      };
    },
    computed: {
      /** 等待时告诉用户在算什么、大概多久（首次要跑分词，全库 3-6 秒） */
      loadingHint() {
        if (this.kind === "report") {
          return "正在统计这一年并提取高频词，首次约 3-6 秒（结果会缓存，再看就秒开）";
        }
        if (this.mode === "contact") {
          return "正在归纳这段关系的互动与心理维度，约 2-5 秒";
        }
        return "正在归纳你的表达风格、作息与常聊话题，首次约 5 秒";
      },
      sectionConf() {
        return this.kind === "profile"
          ? (PROFILE_SECTIONS[this.mode === "self" ? "self" : "contact"] || [])
          : [];
      },
      /** 选中的会话是群聊：互动/情绪指标走「我 vs 群」二分口径，需要免责说明。
          直接读结果体里的 session.type（服务端已判定），不依赖会话列表是否恰好有这一项。 */
      isGroupContact() {
        if (this.kind !== "profile" || this.mode !== "contact" || !this.data) return false;
        return !!(this.data.session && this.data.session.type === "group");
      },
      /** 只呈现后端实际返回了的分区，顺序按配置来 */
      sections() {
        if (!this.data) return [];
        const out = [];
        for (const [key, title, note] of this.sectionConf) {
          const v = this.data[key];
          if (v === undefined || v === null || v === "") continue;
          if (Array.isArray(v) && !v.length) continue;
          if (v && typeof v === "object" && !Array.isArray(v) && !Object.keys(v).length) continue;
          out.push({ key, title, note });
        }
        return out;
      },
      restKeys() {
        if (!this.data) return [];
        const known = new Set(this.sectionConf.map((s) => s[0]));
        return Object.keys(this.data)
          .filter((k) => !known.has(k) && !PROFILE_HIDDEN.has(k));
      },
      restData() {
        return Object.fromEntries(this.restKeys.map((k) => [k, this.data[k]]));
      },
      metaLine() {
        const m = this.data && this.data.meta;
        if (!m) return "";
        const bits = [];
        if (m.scannedMessages) bits.push(`扫描 ${M.fmtNum(m.scannedMessages)} 条消息`);
        if (m.textSampled) bits.push(`抽样 ${M.fmtNum(m.textSampled)} 条文本`);
        /* 触顶说明并入页首脚注，不另起一条 notice：同一件事说两遍是噪音。
           措辞「最近 N 条」与后端口径对齐：contact_profile 已改 order="desc"
           取最近 cap 条（core/profile.py 的 raw_messages_page 调用）。 */
        if (m.truncated) {
          bits.push(`样本已达上限，仅基于最近 ${M.fmtNum(m.maxMsgs || m.scannedMessages)} 条`);
        }
        if (m.cost != null) bits.push(`用时 ${m.cost} 秒`);
        // 命中缓存时明确标注：否则用户会疑惑「上次 5 秒，这次怎么秒开」
        if (m.cached) bits.push("结果来自缓存");
        return bits.length ? bits.join(" · ") + " · 全部本机计算" : "";
      },
    },
    methods: {
      ...{ fmtNum: M.fmtNum },
      /**
       * 会话类型 → 中文。词表只有一份，定义在 views-data.js 的 Contacts 上
       * （后端 `_session_type()` 的值域），这里转调，避免两张表各改各的。
       */
      sessionTypeName(t) { return global.MosuViews.sessionTypeName(t); },
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
            await this.loadProfile();
          } else {
            this.data = await M.api.get("/api/report", this.year ? { year: this.year } : {});
          }
        } catch (e) { this.err = e.message; this.data = null; }
        finally { this.loading = false; }
      },
      /**
       * 画像改走异步任务：contact_profile 要扫全量消息，大群能跑十几秒，
       * 同步持锁会把整个 GUI 卡住且无法超时自救（后端 risk #3）。
       * 轮询写法与 Stats.runKeywords 一致：起一个 taskId，按固定间隔问 /api/task。
       * 不按 `kind` 分流——只认自己刚拿到的那个 taskId 的 status，
       * 所以 kind 新增取值 "profile" 不需要在这里登记，也不会误分流。
       * 服务端不支持 async 时（老版本）返回的是完整结果体，此时直接渲染——优雅降级。
       */
      async loadProfile() {
        const d = await M.api.get("/api/profile", {
          ...(this.picked ? { session: this.picked } : {}), async: 1,
        });
        const tid = d.taskId || d.id || (d.task && d.task.id);
        if (!tid) { this.data = d || null; return; }        // 未走异步分支，直接就是结果
        for (let i = 0; i < 300; i++) {                    // 上限与关键词分析一致：约 7.5 分钟
          await new Promise((r) => setTimeout(r, 1500));
          const t = await M.api.get("/api/task", { id: tid });
          if (t.status === "done") { this.data = t.result || null; return; }
          if (t.status === "error") throw new Error(t.error || "画像生成失败");
        }
        throw new Error("画像生成超时，请重试或缩小数据范围");
      },
    },
    async mounted() {
      const now = new Date().getFullYear();
      this.years = Array.from({ length: 8 }, (_, i) => now - i);
      try {
        const d = await M.api.get("/api/sessions", { limit: 300 });
        this.sessions = d.sessions || [];
      } catch (e) { /* 会话列表拉不到时，联系人画像无法选择，保持空态 */ }
      /* 深链：#/profile?session=<wxid> 直接打开某人的画像。
         没有它时画像链接无法分享——想给朋友看某个人的分析只能口述。 */
      const h = (location.hash || "").match(/[?&]session=([^&]+)/);
      if (h && this.kind === "profile") {
        const id = decodeURIComponent(h[1]);
        this.mode = "contact";
        this.picked = this.sessions.some((s) => s.sessionId === id) ? id : "";
        /* 会话列表没赶上时不猜：picked 为空会停在选择态，比打开错人安全 */
      }
      this.load();
    },
  };

  global.MosuViews = global.MosuViews || {};
  /* isHiddenField 导出去给测试断言：这些内部字段「不该出现在界面上」
     是一条会悄悄失效的规则，值得用测试钉住。 */
  global.MosuViews.__hidden = isHiddenField;
  Object.assign(global.MosuViews, { JsonView, PsychView, Stats, Insight, ReportView });
})(window);
