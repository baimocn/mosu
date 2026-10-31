/* ==========================================================================
   墨溯 v2 · 分享
   合并「导出」「个人语料包」「朋友圈」三块内容，用二级 tab 切换。
   朋友圈原本是独立导航项，但它的本质是"可导出的社交数据"，归到分享下更顺。
   ========================================================================== */
(function (global) {
  "use strict";
  const M = global.Mosu;
  const { ref, computed, onMounted } = Vue;

  const Share = {
    props: { route: { type: String, default: "share" } },
    template: `
    <div class="v2-page v2-scroll">
      <div style="max-width:1000px;margin:0 auto">
        <div class="v2-page-head">
          <h1 class="v2-page-title">分享</h1>
          <p class="v2-page-desc">把数据带走。导出文件只写到你选的目录，不上传任何服务器。</p>
        </div>

        <div class="v2-tabs">
          <button class="v2-tab" :class="{active: tab==='export'}" @click="tab='export'">导出数据</button>
          <button class="v2-tab" :class="{active: tab==='corpus'}" @click="tab='corpus'">个人语料包</button>
          <button class="v2-tab" :class="{active: tab==='moments'}" @click="tab='moments'">朋友圈</button>
        </div>

        <!-- ============ 导出 ============ -->
        <template v-if="tab==='export'">
          <div class="v2-form-layout">
            <div class="v2-card">
              <h2 class="v2-card-title">单会话导出</h2>
              <p class="v2-card-desc" style="margin-bottom:16px">选一个会话，导出为可读格式。</p>

              <div class="v2-field">
                <label class="v2-label">会话</label>
                <select class="v2-select" v-model="form.sessionId">
                  <option value="">— 选择一个会话 —</option>
                  <option v-for="s in sessions" :key="s.sessionId" :value="s.sessionId">
                    {{ s.displayName }}（{{ M.fmtNum(s.messageCount) }} 条）
                  </option>
                </select>
              </div>

              <div class="v2-field">
                <label class="v2-label">格式</label>
                <div class="v2-radio-row">
                  <label v-for="f in formats" :key="f.id" class="v2-radio" :class="{on: form.format===f.id}">
                    <input type="radio" :value="f.id" v-model="form.format" style="display:none" />
                    {{ f.name }}
                  </label>
                </div>
                <div class="v2-hint">{{ formatDesc }}</div>
              </div>

              <div class="v2-field">
                <label class="v2-label">保存位置</label>
                <div class="v2-radio-row">
                  <label v-for="d in dests" :key="d.id" class="v2-radio" :class="{on: form.dest===d.id}">
                    <input type="radio" :value="d.id" v-model="form.dest" style="display:none" />
                    {{ d.name }}
                  </label>
                </div>
                <div class="v2-hint">保存到：{{ curDestPath }}</div>
              </div>

              <div class="v2-narr-cols one" style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
                <div class="v2-field" style="margin:0">
                  <label class="v2-label">起始日期（可选）</label>
                  <input class="v2-input" type="date" v-model="form.from" />
                </div>
                <div class="v2-field" style="margin:0">
                  <label class="v2-label">结束日期（可选）</label>
                  <input class="v2-input" type="date" v-model="form.to" />
                </div>
              </div>

              <button class="v2-btn primary" style="margin-top:16px" :disabled="busy || !form.sessionId"
                      @click="doExport">
                {{ busy ? '导出中…' : '开始导出' }}
              </button>
            </div>

            <div style="display:flex;flex-direction:column;gap:16px">
              <div class="v2-card">
                <h2 class="v2-card-title">批量导出</h2>
                <p class="v2-card-desc" style="margin-bottom:12px">
                  按最近活跃程度导出前 N 个会话，适合喂给 AI 做整体分析。
                </p>
                <div class="v2-field">
                  <label class="v2-label">会话数量（1–30）</label>
                  <input class="v2-input" type="number" min="1" max="30" v-model.number="batch.count" />
                </div>
                <button class="v2-btn" :disabled="busy" @click="doBatch">开始批量导出</button>
              </div>

              <div class="v2-card">
                <h2 class="v2-card-title">历史导出</h2>
                <div v-if="!history.length" class="v2-hint" style="padding:8px 0">还没有导出记录</div>
                <div v-for="(h,i) in history" :key="i" style="padding:8px 0;border-bottom:1px solid var(--border)">
                  <div style="display:flex;justify-content:space-between;gap:8px">
                    <span style="font-size:13px;color:var(--text-1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                      {{ h.name }}
                    </span>
                    <span class="v2-tag">{{ h.size }}</span>
                  </div>
                  <div style="font-size:11px;color:var(--text-4);margin-top:2px">{{ h.path }}</div>
                </div>
              </div>
            </div>
          </div>

          <div class="v2-note" style="margin-top:20px">
            <b>格式说明</b><br/>
            HTML = 聊天气泡样式，适合直接阅读；CSV = 表格，适合 Excel；JSON = 结构化，适合程序处理；
            TXT = 纯文本。
          </div>
        </template>

        <!-- ============ 语料包 ============ -->
        <template v-else-if="tab==='corpus'">
          <div class="v2-card">
            <h2 class="v2-card-title">个人语料包</h2>
            <p class="v2-card-desc" style="margin-bottom:16px">
              全库收集你自己发出的消息，按会话分文件，并蒸馏出一份风格画像（平均长度、表情频率、口头禅等），
              数字分身就是拿这个跑的。
            </p>

            <div v-if="corpus.state && corpus.state.exists">
              <div class="v2-metrics" style="grid-template-columns:repeat(auto-fit,minmax(120px,1fr))">
                <div class="v2-metric accent">
                  <div class="v2-metric-val">{{ M.fmtNum(corpus.state.corpus) }}</div>
                  <div class="v2-metric-label">语料条数</div>
                </div>
                <div class="v2-metric">
                  <div class="v2-metric-val">{{ M.fmtNum(corpus.state.sessions) }}</div>
                  <div class="v2-metric-label">覆盖会话</div>
                </div>
                <div class="v2-metric">
                  <div class="v2-metric-val" style="font-size:16px">
                    {{ corpus.state.styleProfile ? '已生成' : '未生成' }}
                  </div>
                  <div class="v2-metric-label">风格画像</div>
                </div>
                <div class="v2-metric">
                  <div class="v2-metric-val" style="font-size:16px">{{ corpus.state.mtime || '—' }}</div>
                  <div class="v2-metric-label">生成时间</div>
                </div>
              </div>

              <div style="display:flex;gap:8px;margin-top:20px;flex-wrap:wrap">
                <button class="v2-btn" :disabled="busy" @click="regen">
                  {{ busy ? '生成中…' : '重新生成' }}
                </button>
                <button class="v2-btn" @click="preview = !preview">
                  {{ preview ? '收起预览' : '预览风格画像' }}
                </button>
                <a class="v2-btn" :href="'/api/persona-corpus?download=1'" download>
                  下载语料包
                </a>
              </div>

              <div v-if="preview" class="v2-card" style="margin-top:16px;background:var(--bg-surface-2)">
                <pre style="margin:0;font-family:var(--font-mono);font-size:12px;line-height:1.7;
                            white-space:pre-wrap;color:var(--text-2)">{{ styleText }}</pre>
              </div>
            </div>
            <div v-else>
              <div class="v2-note warn" style="margin-bottom:16px">
                语料包还不存在。生成一次通常需要十几秒到几分钟，取决于全库消息量。
              </div>
              <button class="v2-btn primary" :disabled="busy" @click="regen">
                {{ busy ? '生成中…' : '生成语料包' }}
              </button>
            </div>
          </div>
        </template>

        <!-- ============ 朋友圈 ============ -->
        <template v-else>
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;gap:12px;flex-wrap:wrap">
            <div>
              <h2 class="v2-card-title">朋友圈</h2>
              <p class="v2-card-desc" style="margin-top:4px">
                共 {{ M.fmtNum(moments.total) }} 条，已渲染 {{ moments.shown }} 条
                <template v-if="moments.skipped"> · {{ moments.skipped }} 条未渲染</template>
              </p>
            </div>
            <div style="display:flex;gap:8px;align-items:center">
              <input class="v2-input" style="width:180px" v-model="momentsFilter"
                     placeholder="按作者筛选…" />
              <button class="v2-btn" @click="loadMoments">刷新</button>
            </div>
          </div>

          <div class="v2-note warn" style="margin-bottom:16px">
            图片需要微信端仍持有有效的访问凭据。本机不常驻图片密钥时，图片无法解密，
            只显示占位——这是能力边界，不是故障。
          </div>

          <div v-if="moments.loading" class="v2-wall">
            <div v-for="i in 6" :key="i" class="v2-skel" style="height:180px;border-radius:14px"></div>
          </div>
          <div v-else-if="!moments.entries.length" class="v2-empty">
            <div class="v2-empty-title">没有朋友圈记录</div>
          </div>
          <div v-else class="v2-wall">
            <div v-for="(e,i) in filteredMoments" :key="i" class="v2-wall-item">
              <div class="v2-wall-head">
                <div class="v2-avatar sm" :style="{background: avatarColor(e.authorName)}">
                  {{ initial(e.authorName) }}
                </div>
                <div style="min-width:0">
                  <div class="v2-wall-name">{{ e.authorName || '未知' }}</div>
                  <div class="v2-wall-time">{{ fmtTime(e.createTime) }}</div>
                </div>
              </div>
              <div v-if="e.text" class="v2-wall-text">{{ e.text }}</div>
              <div v-if="e.media && e.media.length" class="v2-wall-media" :class="'n'+Math.min(4, e.media.length)">
                <div v-for="(m,j) in e.media" :key="j" class="v2-media"
                     :style="{aspectRatio:'1/1',minHeight:'80px'}">
                  <img v-if="m.flags && m.flags.includes('remote') && m.url" :src="m.url"
                       loading="lazy" @error="onImgErr" alt="" />
                  <span v-else-if="m.flags && m.flags.includes('video')">🎬 视频</span>
                  <span v-else>图片不可用</span>
                </div>
              </div>
              <div v-if="e.likes || e.comments" style="margin-top:10px;font-size:11px;color:var(--text-4)">
                <span v-if="e.likes">♥ {{ e.likes.length }}</span>
                <span v-if="e.comments" style="margin-left:10px">💬 {{ e.comments.length }}</span>
              </div>
            </div>
          </div>
        </template>
      </div>
    </div>`,
    setup(props) {
      const tab = ref("export");
      const busy = ref(false);
      const sessions = computed(() => M.store.sessions);
      const history = ref([]);
      const dests = ref([]);
      const destPath = ref("");
      const corpus = ref({});
      const styleText = ref("");
      const preview = ref(false);
      const momentsFilter = ref("");
      const moments = ref({ entries: [], total: 0, shown: 0, skipped: 0, loading: false });

      const form = ref({ sessionId: "", format: "html", dest: "project", from: "", to: "" });
      const batch = ref({ count: 3 });

      const formats = [
        { id: "html", name: "HTML" },
        { id: "csv", name: "CSV" },
        { id: "json", name: "JSON" },
        { id: "txt", name: "TXT" },
      ];      const curDestPath = computed(() => {
        const d = dests.value.find((x) => x.id === form.value.dest);
        return (d && d.path) || destPath.value || "—";
      });

      const formatDesc = computed(() => ({
        html: "聊天气泡样式，含图片引用，可直接双击打开。",
        csv: "表格，一行一条消息，适合 Excel。",
        json: "结构化数据，字段完整，适合程序处理。",
        txt: "纯文本，按时间顺序排列。",
      }[form.value.format]));

      const filteredMoments = computed(() => {
        const kw = momentsFilter.value.trim().toLowerCase();
        if (!kw) return moments.value.entries;
        return moments.value.entries.filter((e) =>
          String(e.authorName || "").toLowerCase().includes(kw));
      });

      async function loadHistory() {
        try {
          const d = await M.api.get("/api/exports", { limit: 10 });
          history.value = (d.exports || d.items || []).map((x) => ({
            name: x.name || x.filename || "导出",
            path: x.path || x.dir || "",
            size: M.fmtSize(x.size || 0),
          }));
        } catch (e) { /* 忽略 */ }
      }

      async function loadDests() {
        try {
          const d = await M.api.get("/api/export/dests");
          // 后端字段是 key/label/path，不是 id/name
          dests.value = (d.dests || []).map((x) => ({
            id: x.key, name: x.label, path: x.path, exists: x.exists,
          }));
          destPath.value = (dests.value[0] && dests.value[0].path) || "";
        } catch (e) {
          dests.value = [{ id: "project", name: "项目目录", path: "", exists: true }];
        }
      }

      async function loadCorpus() {
        try { corpus.value = await M.api.get("/api/persona-corpus"); } catch (e) { /* 忽略 */ }
      }

      async function loadStyle() {
        try {
          const d = await M.api.get("/api/persona-corpus/md");
          styleText.value = d.markdown || d.text || d.content || "";
        } catch (e) { styleText.value = "（预览加载失败）"; }
      }

      async function regen() {
        busy.value = true;
        try {
          const d = await M.api.post("/api/persona-corpus", { regen: true });
          if (d.error) throw new Error(d.error);
          await loadCorpus();
          await loadStyle();
          M.toast.ok("语料包已生成");
        } catch (e) {
          M.toast.err("生成失败：" + e.message);
        } finally {
          busy.value = false;
        }
      }

      async function doExport() {
        busy.value = true;
        try {
          const payload = Object.assign({}, form.value);
          if (!payload.from) delete payload.from;
          if (!payload.to) delete payload.to;
          const d = await M.api.post("/api/export", payload);
          if (d.error) throw new Error(d.error);
          M.toast.ok("导出完成：" + (d.name || d.path || "已保存"));
          loadHistory();
        } catch (e) {
          M.toast.err("导出失败：" + e.message);
        } finally {
          busy.value = false;
        }
      }

      async function doBatch() {
        busy.value = true;
        try {
          const d = await M.api.post("/api/export", { batch: true, count: batch.value.count });
          if (d.error) throw new Error(d.error);
          M.toast.ok("批量导出完成");
          loadHistory();
        } catch (e) {
          M.toast.err("批量导出失败：" + e.message);
        } finally {
          busy.value = false;
        }
      }

      async function loadMoments() {
        moments.value = Object.assign({}, moments.value, { loading: true });
        try {
          const d = await M.api.get("/api/moments", { limit: 60 });
          const entries = d.entries || [];
          moments.value = {
            entries: entries,
            total: d.total || entries.length,
            shown: entries.length,
            skipped: Math.max(0, (d.total || 0) - entries.length),
            loading: false,
          };
        } catch (e) {
          moments.value = { entries: [], total: 0, shown: 0, skipped: 0, loading: false };
          M.toast.err("加载朋友圈失败：" + e.message);
        }
      }

      function onImgErr(ev) {
        const img = ev.target;
        if (img) {
          img.removeAttribute("src");
          img.parentElement.textContent = "图片不可用";
        }
      }

      onMounted(async () => {
        if (props.route && props.route.includes(".")) {
          const t = props.route.split(".")[1];
          if (t === "corpus" || t === "moments") tab.value = t;
        }
        await Promise.all([loadHistory(), loadDests(), loadCorpus()]);
        const pending = global.MosuV2.pendingExportSession;
        if (pending) {
          global.MosuV2.pendingExportSession = null;
          form.value.sessionId = pending;
        }
        if (tab.value === "moments") await loadMoments();
        if (tab.value === "corpus" && corpus.value.state && corpus.value.state.styleProfile) loadStyle();
      });

      return {
        tab, busy, sessions, history, dests, destPath, corpus, styleText, preview,
        moments, momentsFilter, filteredMoments, form, batch, formats, formatDesc,
        curDestPath,
        doExport, doBatch, regen, loadMoments, loadStyle, onImgErr, M,
        avatarColor: M.avatarColor, initial: M.initial, fmtTime: M.fmtTime,
      };
    },
  };

  global.MosuV2 = global.MosuV2 || {};
  global.MosuV2.Share = Share;
})(window);
