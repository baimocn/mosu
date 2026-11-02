/* ==========================================================================
   墨溯 v2 · 模型选择器
   对标 Lancer 的 features/conversation/model-reel.tsx（家族图标条 + 分组列表）。

   三条设计约束（都是踩过才知道的）：
   1. 家族归类按**模型名**，不按端点名。用户拿兼容网关转发 Claude，
      端点叫 openai，模型必须显示成 Claude。规则在后端 core/model_catalog.py。
   2. 家族按钮带 data-anchor-key="family:<id>"——与分支树、光线锚点
      同一套语义锚点体系，Playwright 端到端靠它定位。
   3. 三态必须一眼可分：可用（可点）/ 未启用（灰，不可点）/ 未配置（灰 + 标签）。
      只用颜色区分不够——色弱用户和截图都看不出来，所以状态带文字。
   ========================================================================== */
(function (global) {
  "use strict";
  const M = global.Mosu;
  const { ref, computed, onMounted } = Vue;

  const ModelPicker = {
    props: {
      // "full" = 完整面板（设置页用）；"bar" = 只显示当前模型 + 弹出层
      mode: { type: String, default: "full" },
      compact: { type: Boolean, default: false },
    },
    template: `
    <div class="v2-mp" :class="'mode-' + mode">
      <!-- 当前模型条
           注意：**full 模式也显示**（迭代 13 改）。
           原来只在 bar 模式显示，而 bar 模式压根没在任何页面挂载
           （只有 settings.js 挂了 mode="full"）→
           这套「当前模型徽标」代码从来没在真机上渲染过一次，
           于是「切换后徽标认不出当前模型」这个 bug 一直没人看见。
           用户在设置页选模型时，最需要知道的就是「现在在用哪个」。 -->
      <div class="v2-mp-current" v-if="mode==='bar' || currentLabel">
        <button class="v2-mp-current-btn" @click="open = !open"
                :title="currentTitle">
          <span class="v2-mp-current-badge"
                :style="badgeStyle(currentFamily)">{{ currentFamily.badge }}</span>
          <span class="v2-mp-current-name">{{ currentLabel }}</span>
          <span class="v2-mp-caret">▾</span>
        </button>
      </div>

      <div class="v2-mp-panel" :class="{open: mode==='full' || open}">
        <!-- 家族图标条 -->
        <div class="v2-mp-families" role="tablist" aria-label="模型家族">
          <button v-for="f in familyList" :key="f.id || 'none'"
                  class="v2-mp-fam"
                  :class="{active: famId===f.id, unconfigured: f.modelCount && !f.count}"
                  role="tab"
                  :aria-selected="famId===f.id"
                  :data-anchor-key="'family:' + (f.id || 'none')"
                  :title="f.label + ' · ' + f.count + ' 个可用'"
                  @click="famId = f.id">
            <span class="v2-mp-fam-badge" :style="{background: f.color}">{{ f.badge }}</span>
            <span class="v2-mp-fam-label">{{ f.label }}</span>
            <span class="v2-mp-fam-count" v-if="f.modelCount">{{ f.count }}/{{ f.modelCount }}</span>
          </button>
        </div>

        <!-- 搜索 -->
        <div class="v2-mp-search" v-if="activeFamily.modelCount > 6">
          <input class="v2-input" type="search" v-model="query"
                 placeholder="搜索模型" aria-label="搜索模型" />
        </div>

        <!-- 模型列表 -->
        <div class="v2-mp-list">
          <button v-for="m in visibleModels" :key="m.providerId + '/' + m.modelId"
                  class="v2-mp-item"
                  :class="[m.state, {active: isActive(m)}]"
                  :disabled="m.state!=='available'"
                  :data-model-id="m.modelId"
                  :data-endpoint-id="m.providerId"
                  :data-state="m.state"
                  @click="activate(m)">
            <span class="v2-mp-item-badge" :style="{background: m.familyColor}">{{ m.familyBadge }}</span>
            <span class="v2-mp-item-main">
              <span class="v2-mp-item-label">{{ m.label }}</span>
              <span class="v2-mp-item-sub">{{ m.providerName }} · {{ m.modelId }}</span>
            </span>
            <span class="v2-mp-item-state" v-if="m.state!=='available'">{{ m.stateLabel }}</span>
            <span class="v2-mp-item-check" v-else-if="isActive(m)">✓</span>
          </button>

          <div v-if="!activeFamily.modelCount" class="v2-mp-empty">
            尚未配置此模型家族。可在下方添加端点，或用「探测」拉取该端点的模型列表。
          </div>
          <div v-else-if="!visibleModels.length" class="v2-mp-empty">没有匹配的模型。</div>
        </div>

        <!-- 端点管理（仅 full 模式） -->
        <div class="v2-mp-endpoints" v-if="mode==='full'">
          <div class="v2-mp-ep-head">
            <span>端点（{{ endpoints.length }}）</span>
            <button class="v2-btn sm" @click="showForm = !showForm">
              {{ showForm ? '收起' : '+ 添加端点' }}
            </button>
          </div>

          <div class="v2-mp-ep-list">
            <div v-for="e in endpoints" :key="e.id" class="v2-mp-ep"
                 :class="{active: e.active}">
              <div class="v2-mp-ep-main">
                <div class="v2-mp-ep-label">
                  {{ e.label }}
                  <span class="v2-mp-ep-state" :class="e.state">{{ e.stateLabel }}</span>
                </div>
                <div class="v2-mp-ep-url">{{ e.baseUrl }}</div>
                <div class="v2-mp-ep-meta">{{ e.modelCount }} 个模型{{ e.hasKey ? '' : ' · 缺 API Key' }}</div>
              </div>
              <div class="v2-mp-ep-ops">
                <button class="v2-btn sm" @click="editEndpoint(e)">编辑</button>
                <button class="v2-btn sm danger" @click="removeEndpoint(e)">删除</button>
              </div>
            </div>
            <div v-if="!endpoints.length" class="v2-mp-empty">
              还没有端点。墨溯不内置任何服务商地址——请填你自己的。
            </div>
          </div>

          <!-- 端点表单 -->
          <div class="v2-mp-form" v-if="showForm">
            <div class="v2-field">
              <label class="v2-label">名称</label>
              <input class="v2-input" v-model="form.label" placeholder="如：DeepSeek 官方" />
            </div>
            <div class="v2-field">
              <label class="v2-label">接口地址</label>
              <input class="v2-input" v-model="form.baseUrl" placeholder="https://api.deepseek.com/v1" />
            </div>
            <div class="v2-field">
              <label class="v2-label">API Key</label>
              <input class="v2-input" type="password" v-model="form.apiKey" placeholder="sk-…" />
            </div>
            <div class="v2-field">
              <label class="v2-label">模型名（每行一个，可留空后用探测）</label>
              <textarea class="v2-input" v-model="form.modelsText" rows="3"
                        placeholder="deepseek-chat&#10;deepseek-reasoner"></textarea>
            </div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
              <button class="v2-btn" :disabled="busy" @click="probe">探测模型列表</button>
              <button class="v2-btn primary" :disabled="busy" @click="saveEndpoint">保存端点</button>
              <button class="v2-btn ghost" v-if="editingId" @click="resetForm">取消编辑</button>
              <span class="v2-hint" v-if="probeHint">{{ probeHint }}</span>
            </div>
          </div>

          <!-- 启用名单（迭代 13 补：原来后端接口有、前端零入口，
               没人能从界面设置「只启用哪几个模型」）。
               留空 = 全部启用。支持 * / provider/* / provider/model 三种写法。 -->
          <div class="v2-mp-enabled">
            <div class="v2-mp-enabled-head">
              <span>启用名单</span>
              <span class="v2-hint">留空 = 全部启用。支持 deepseek-* 、 ep_x/* 、 ep_x/deepseek-chat</span>
            </div>
            <textarea class="v2-input" v-model="enabledText" rows="3"
                      placeholder="留空即全部启用&#10;deepseek-*&#10;ep_ab12/glm-5"></textarea>
            <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
              <button class="v2-btn primary" :disabled="busy" @click="saveEnabled">保存名单</button>
              <button class="v2-btn ghost" :disabled="busy" @click="clearEnabled">全部启用</button>
              <span class="v2-hint" v-if="enabledHint">{{ enabledHint }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>`,
    setup(props) {
      const catalog = ref(null);
      const famId = ref("");
      const query = ref("");
      const open = ref(false);
      const busy = ref(false);
      const showForm = ref(false);
      const editingId = ref("");
      const probeHint = ref("");
      const form = ref({ label: "", baseUrl: "", apiKey: "", modelsText: "" });
      // 启用名单（迭代 13 补）
      const enabledText = ref("");
      const enabledHint = ref("");

      const families = computed(() => (catalog.value && catalog.value.families) || []);
      const endpoints = computed(() => (catalog.value && catalog.value.endpoints) || []);

      // 只展示有模型的家族（没配过的家族点了也是空的，浪费点击）
      const familyList = computed(() => families.value.filter((f) => f.modelCount > 0));

      // 当前选中的家族（决定列表显示哪一组）
      // 名字不能叫 currentFamily —— 下面还有一个「当前活跃模型所属家族」
      // 同名 const 在同一个 setup 作用域里是 SyntaxError，整个组件直接挂掉。
      const activeFamily = computed(() => {
        const list = families.value;
        let f = list.find((x) => x.id === famId.value);
        if (!f) f = list.find((x) => x.modelCount > 0) || list[0] || null;
        return f || { id: "", label: "", badge: "?", color: "#8a8a8a",
                      count: 0, modelCount: 0, models: [] };
      });

      const visibleModels = computed(() => {
        const q = (query.value || "").trim().toLowerCase();
        const models = activeFamily.value.models || [];
        if (!q) return models;
        return models.filter((m) =>
          (`${m.label} ${m.providerName} ${m.modelId}`).toLowerCase().includes(q));
      });

      const active = computed(() => (catalog.value && catalog.value.active) || null);
      const currentLabel = computed(() => {
        if (!active.value) return "选择模型";
        const eps = endpoints.value;
        const ep = eps.find((e) => e.id === active.value.endpointId);
        const epLabel = ep ? ep.label : active.value.endpointId;
        return `${epLabel} · ${active.value.modelId}`;
      });
      const currentFamily = computed(() => {
        const cur = catalog.value && catalog.value.current;
        const fams = families.value;
        if (!cur || !cur.model) return { badge: "?", color: "#8a8a8a", label: "" };
        // 先按「活跃端点 + 模型 id」精确找——同一个模型 id 可能挂在多个端点下
        // （两个端点都有 deepseek-chat），只按模型 id 找会取到错的那个家族。
        const act = active.value;
        let fam = null;
        for (const f of fams) {
          const hit = (f.models || []).some((m) =>
            m.modelId === cur.model &&
            (!act || m.providerId === act.endpointId));
          if (hit) { fam = f; break; }
        }
        if (!fam) {
          for (const f of fams) {
            if ((f.models || []).some((m) => m.modelId === cur.model)) { fam = f; break; }
          }
        }
        return fam || { badge: "?", color: "#8a8a8a", label: "" };
      });

      function badgeStyle(f) {
        return f && f.badge ? { background: f.color } : {};
      }
      function isActive(m) {
        return !!active.value && active.value.endpointId === m.providerId
          && active.value.modelId === m.modelId;
      }

      async function load() {
        try {
          const d = await M.api.get("/api/models");
          if (d && !d.error) {
            catalog.value = d;
            // 启用名单跟着 catalog 一起带回来（迭代 13）：
            // 后端把 enabledPatterns 放进 catalog，前端一次 GET 就拿到。
            enabledText.value = (d.enabledPatterns || []).join("\n");
          }
        } catch (e) { /* 忽略 */ }
      }

      // 保存启用名单（迭代 13）。
      //
      // 为什么按行拆而不是按逗号：模型 id 里可能带逗号吗？不会，
      // 但**用户习惯按行粘贴**（从文档里复制一行一个），
      // 按行拆最符合直觉；逗号分隔的也一并接受，两种写法混用不会出错。
      function _parsePatterns(text) {
        const out = [];
        for (const line of String(text || "").split(/[\n,]/)) {
          const s = line.trim();
          if (s) out.push(s);
        }
        return out;
      }

      async function saveEnabled() {
        busy.value = true;
        enabledHint.value = "";
        try {
          const pats = _parsePatterns(enabledText.value);
          const d = await M.api.post("/api/models/enabled", { patterns: pats });
          if (d.error) throw new Error(d.error);
          if (d.catalog) catalog.value = d.catalog;
          enabledText.value = (d.patterns || []).join("\n");
          enabledHint.value = pats.length
            ? "已启用 " + pats.length + " 条规则"
            : "已恢复为全部启用";
          M.toast.ok(enabledHint.value);
        } catch (e) {
          enabledHint.value = "保存失败：" + e.message;
          M.toast.err(enabledHint.value);
        } finally {
          busy.value = false;
        }
      }

      async function clearEnabled() {
        enabledText.value = "";
        await saveEnabled();
      }

      async function activate(m) {
        if (m.state !== "available") return;
        busy.value = true;
        try {
          const d = await M.api.post("/api/models/activate", {
            endpointId: m.providerId, modelId: m.modelId,
          });
          if (d.error) throw new Error(d.error);
          if (d.catalog) catalog.value = d.catalog;
          M.toast.ok("已切换到 " + m.label);
          open.value = false;
        } catch (e) {
          M.toast.err("切换失败：" + e.message);
        } finally {
          busy.value = false;
        }
      }

      function resetForm() {
        form.value = { label: "", baseUrl: "", apiKey: "", modelsText: "" };
        editingId.value = "";
        probeHint.value = "";
      }

      function editEndpoint(e) {
        editingId.value = e.id;
        form.value = { label: e.label, baseUrl: e.baseUrl, apiKey: "",
                       modelsText: "" };
        probeHint.value = "";
        showForm.value = true;
      }

      async function probe() {
        busy.value = true;
        probeHint.value = "探测中…";
        try {
          const d = await M.api.post("/api/models/probe", {
            baseUrl: form.value.baseUrl.trim(), apiKey: form.value.apiKey.trim(),
          });
          if (d.ok) {
            form.value.modelsText = (d.models || []).join("\n");
            probeHint.value = "拉到 " + d.models.length + " 个模型";
          } else {
            probeHint.value = "探测失败：" + (d.error || "") + " " + (d.hint || "");
          }
        } catch (e) {
          probeHint.value = "探测异常：" + e.message;
        } finally {
          busy.value = false;
        }
      }

      async function saveEndpoint() {
        busy.value = true;
        try {
          const models = form.value.modelsText.split("\n")
            .map((s) => s.trim()).filter(Boolean);
          const payload = {
            id: editingId.value,
            label: form.value.label.trim(),
            baseUrl: form.value.baseUrl.trim(),
            apiKey: form.value.apiKey.trim(),
            models: models,
          };
          const d = await M.api.post("/api/models/endpoint", { endpoint: payload });
          if (d.error) throw new Error(d.error);
          if (d.catalog) catalog.value = d.catalog;
          resetForm();
          showForm.value = false;
          M.toast.ok("端点已保存");
        } catch (e) {
          M.toast.err("保存失败：" + e.message);
        } finally {
          busy.value = false;
        }
      }

      async function removeEndpoint(e) {
        if (!global.confirm("删除端点「" + e.label + "」？")) return;
        busy.value = true;
        try {
          const d = await M.api.post("/api/models/endpoint/delete", { id: e.id });
          if (d.error) throw new Error(d.error);
          if (d.catalog) catalog.value = d.catalog;
          M.toast.ok("已删除");
        } catch (err) {
          M.toast.err("删除失败：" + err.message);
        } finally {
          busy.value = false;
        }
      }

      onMounted(load);

      return {
        catalog, families, endpoints, familyList, activeFamily, currentFamily,
        visibleModels,
        currentLabel, famId, query, open, busy, showForm, editingId, form,
        probeHint, badgeStyle, isActive, activate, probe, saveEndpoint,
        removeEndpoint, editEndpoint, resetForm, load, M,
        // 启用名单（迭代 13 补）——模板里调了必须在 setup 里 return
        enabledText, enabledHint, saveEnabled, clearEnabled,
      };
    },
  };

  global.MosuV2 = global.MosuV2 || {};
  global.MosuV2.ModelPicker = ModelPicker;
})(window);
