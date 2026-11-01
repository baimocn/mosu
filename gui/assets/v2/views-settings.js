/* ==========================================================================
   墨溯 v2 · 设置中心
   把「账号与密钥」「微信通道」「MCP 服务」三个原本平级的导航项收进二级 tab。
   这些是"配一次就不用管"的设置，不该跟"聊天""洞察"抢主导航。
   ========================================================================== */
(function (global) {
  "use strict";
  const M = global.Mosu;
  const { ref, computed, onMounted } = Vue;

  const TABS = [
    { id: "account", name: "账号与密钥" },
    { id: "channel", name: "微信通道" },
    { id: "mcp", name: "MCP 服务" },
    { id: "model", name: "大模型" },
  ];

  const Settings = {
    props: { route: { type: String, default: "settings" } },
    // 踩过的坑（迭代 4）：只在上层 shell.js 注册 → 子组件里的 <model-picker>
    // 成了未知标签，Vue 静默忽略不报错，页面看着像"组件没渲染"。
    // 局部注册是"向下可见"，自己用自己注册最保险。
    components: { "model-picker": global.MosuV2.ModelPicker },
    template: `
    <div class="v2-page v2-scroll">
      <div style="max-width:1000px;margin:0 auto">
        <div class="v2-page-head">
          <h1 class="v2-page-title">设置</h1>
          <p class="v2-page-desc">配置只保存在本机。密钥存在 <code style="font-size:12px">~/.mosu/config.json</code>，不上传。</p>
        </div>

        <div class="v2-settings-layout">
          <nav class="v2-settings-nav">
            <button v-for="t in tabs" :key="t.id" :class="{active: tab===t.id}" @click="tab=t.id">
              {{ t.name }}
            </button>
          </nav>

          <div>
            <!-- ============ 账号与密钥 ============ -->
            <template v-if="tab==='account'">
              <div class="v2-card" style="margin-bottom:16px">
                <h2 class="v2-card-title">当前账号</h2>
                <div class="v2-kv" style="margin-top:10px"><span class="k">账号 ID</span><span class="v">{{ config.current || '—' }}</span></div>
                <div class="v2-kv"><span class="k">数据库路径</span><span class="v">{{ config.dbPath || '—' }}</span></div>
                <div class="v2-kv"><span class="k">数据库密钥</span><span class="v">{{ config.hasKey ? config.keyPreview : '未配置' }}</span></div>
                <div class="v2-kv"><span class="k">图片密钥</span><span class="v">{{ config.hasImgKey ? '已配置' : '未配置（图片无法解密）' }}</span></div>
                <div class="v2-kv"><span class="k">配置目录</span><span class="v">{{ config.mosuHome || '—' }}</span></div>
              </div>

              <div class="v2-card" style="margin-bottom:16px">
                <h2 class="v2-card-title">密钥</h2>
                <p class="v2-card-desc" style="margin-bottom:14px">
                  微信 4.1 的数据库密钥与图片密钥彼此独立，无派生关系。图片密钥不常驻，需先在微信里打开一张图。
                </p>
                <div class="v2-field">
                  <label class="v2-label">数据库密钥（64 位 hex）</label>
                  <input class="v2-input" v-model="keyHex" placeholder="64 位十六进制" />
                </div>
                <div class="v2-field">
                  <label class="v2-label">图片密钥（32 位 hex，可选）</label>
                  <input class="v2-input" v-model="imgKeyHex" placeholder="32 位十六进制" />
                </div>
                <div style="display:flex;gap:8px;flex-wrap:wrap">
                  <button class="v2-btn primary" :disabled="busy" @click="saveKeys">保存密钥</button>
                  <button class="v2-btn" :disabled="busy" @click="selftest">验证密钥</button>
                  <button class="v2-btn ghost" @click="keyHex='';imgKeyHex=''">清空</button>
                </div>
                <div v-if="keyMsg" class="v2-note" style="margin-top:14px"
                     :class="keyOk ? 'ok' : 'danger'">{{ keyMsg }}</div>
              </div>

              <div class="v2-card">
                <h2 class="v2-card-title">多账号</h2>
                <p class="v2-card-desc" style="margin-bottom:14px">在多套微信数据之间切换。</p>
                <div v-for="a in accounts" :key="a.name" style="display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid var(--border)">
                  <span style="flex:1;font-size:13px;color:var(--text-1)">{{ a.name }}</span>
                  <template v-if="a.current">
                    <span class="v2-tag brand">当前</span>
                  </template>
                  <template v-else>
                    <button class="v2-btn sm" @click="useAccount(a.name)">切换</button>
                    <button class="v2-btn sm danger" @click="removeAccount(a.name)">删除</button>
                  </template>
                </div>
                <div style="display:flex;gap:8px;margin-top:14px">
                  <input class="v2-input" v-model="newAccountId" placeholder="新账号 wxid" />
                  <input class="v2-input" v-model="newAccountPath" placeholder="数据库目录路径" />
                  <button class="v2-btn" :disabled="!newAccountId || !newAccountPath" @click="addAccount">添加</button>
                </div>
              </div>
            </template>

            <!-- ============ 微信通道 ============ -->
            <template v-else-if="tab==='channel'">
              <div class="v2-card" style="margin-bottom:16px">
                <h2 class="v2-card-title">通道状态</h2>
                <div class="v2-kv" style="margin-top:10px"><span class="k">登录状态</span><span class="v">{{ wx.logged_in ? '已连接' : '未登录' }}</span></div>
                <div class="v2-kv"><span class="k">账号</span><span class="v">{{ wx.account || '—' }}</span></div>
                <div class="v2-kv"><span class="k">长轮询服务</span><span class="v">{{ svc.alive ? '运行中' : '未启动' }}</span></div>
                <div class="v2-kv"><span class="k">上次活动</span><span class="v">{{ svc.last_seen ? fmtTime(svc.last_seen) : '—' }}</span></div>
              </div>

              <div class="v2-card" style="margin-bottom:16px">
                <h2 class="v2-card-title">登录</h2>
                <p class="v2-card-desc" style="margin-bottom:14px">
                  扫码后凭据存在本机，不经过第三方服务器中转。
                </p>
                <div v-if="qrSrc" style="text-align:center;margin-bottom:14px">
                  <img :src="qrSrc" alt="登录二维码" style="width:220px;height:220px;border-radius:12px;border:1px solid var(--border)" />
                  <div style="font-size:12px;color:var(--text-4);margin-top:8px">{{ qrStatus }}</div>
                </div>
                <div style="display:flex;gap:8px;flex-wrap:wrap">
                  <button class="v2-btn primary" :disabled="busy" @click="startLogin">
                    {{ qrSrc ? '重新获取二维码' : '获取登录二维码' }}
                  </button>
                  <button class="v2-btn" :disabled="!qrSrc" @click="pollOnce">检查扫码结果</button>
                  <button class="v2-btn ghost" @click="wxAction('start')">启动常驻服务</button>
                  <button class="v2-btn ghost" @click="wxAction('stop')">停止常驻服务</button>
                </div>
              </div>

              <div class="v2-card">
                <h2 class="v2-card-title">消息流水</h2>
                <p class="v2-card-desc" style="margin-bottom:12px">最近 50 条通道日志。</p>
                <div style="max-height:320px;overflow-y:auto" class="v2-scroll">
                  <div v-for="(l,i) in logs" :key="i" style="padding:6px 0;border-bottom:1px solid var(--border);font-size:12px;color:var(--text-2);font-family:var(--font-mono)">
                    {{ l }}
                  </div>
                  <div v-if="!logs.length" class="v2-hint">暂无日志</div>
                </div>
                <button class="v2-btn sm" style="margin-top:12px" @click="loadLogs">刷新日志</button>
              </div>
            </template>

            <!-- ============ MCP ============ -->
            <template v-else-if="tab==='mcp'">
              <div class="v2-card" style="margin-bottom:16px">
                <h2 class="v2-card-title">MCP 服务</h2>
                <p class="v2-card-desc" style="margin-bottom:14px">
                  把墨溯的 {{ mcpTools.length }} 个工具挂给 AI 客户端（Claude Desktop / Cursor 等）。
                </p>
                <div class="v2-field">
                  <label class="v2-label">客户端配置片段</label>
                  <pre style="margin:0;padding:14px;background:var(--bg-inset);border-radius:10px;
                              font-family:var(--font-mono);font-size:11px;line-height:1.7;
                              white-space:pre-wrap;color:var(--text-2);overflow-x:auto">{{ mcpSnippet }}</pre>
                </div>
                <div style="display:flex;gap:8px;flex-wrap:wrap">
                  <button class="v2-btn" @click="copySnippet">复制配置</button>
                  <button class="v2-btn" :disabled="busy" @click="mcpSelftest">自检</button>
                </div>
                <div v-if="mcpMsg" class="v2-note" style="margin-top:14px" :class="mcpOk ? 'ok' : 'danger'">{{ mcpMsg }}</div>
              </div>

              <div class="v2-card">
                <h2 class="v2-card-title">可用工具（{{ mcpTools.length }}）</h2>
                <div style="margin-top:10px">
                  <div v-for="t in mcpTools" :key="t.name" style="padding:9px 0;border-bottom:1px solid var(--border)">
                    <div style="font-size:13px;font-family:var(--font-mono);color:var(--brand)">{{ t.name }}</div>
                    <div style="font-size:12px;color:var(--text-3);margin-top:2px">{{ t.description }}</div>
                  </div>
                </div>
              </div>
            </template>

            <!-- ============ 大模型 ============ -->
            <template v-else>
              <div class="v2-card" style="margin-bottom:16px">
                <h2 class="v2-card-title">大模型配置</h2>
                <p class="v2-card-desc" style="margin-bottom:14px">
                  用于数字分身对话与关键词提取。留空则相关功能不可用。
                </p>
                <div class="v2-field">
                  <label class="v2-label">接口地址</label>
                  <input class="v2-input" v-model="llm.baseUrl" placeholder="https://api.example.com/v1" />
                </div>
                <div class="v2-field">
                  <label class="v2-label">API Key</label>
                  <input class="v2-input" type="password" v-model="llm.apiKey" placeholder="sk-…" />
                </div>
                <div class="v2-field">
                  <label class="v2-label">模型名</label>
                  <input class="v2-input" v-model="llm.model" placeholder="deepseek-v4.1-flash" />
                </div>
                <button class="v2-btn primary" :disabled="busy" @click="saveLlm">保存</button>
                <div v-if="llmMsg" class="v2-note" style="margin-top:14px" :class="llmOk ? 'ok' : 'danger'">{{ llmMsg }}</div>
              </div>

              <!-- 模型目录：多端点 + 家族分组 + 三态（对标 Lancer model-reel） -->
              <div class="v2-card">
                <h2 class="v2-card-title">模型目录</h2>
                <p class="v2-card-desc" style="margin-bottom:14px">
                  可挂多个端点，切换模型在下面点一下即可（会同时改写上面的单项配置）。
                  家族按**模型名**归类——用兼容网关转发 Claude，家族仍显示 Claude。
                </p>
                <model-picker mode="full"></model-picker>
              </div>
            </template>
          </div>
        </div>
      </div>
    </div>`,
    setup(props) {
      const tab = ref("account");
      const busy = ref(false);
      const keyHex = ref("");
      const imgKeyHex = ref("");
      const keyMsg = ref("");
      const keyOk = ref(false);
      const mcpMsg = ref("");
      const mcpOk = ref(false);
      const llmMsg = ref("");
      const llmOk = ref(false);
      const mcpTools = ref([]);
      const qrSrc = ref("");
      const qrId = ref("");
      const qrStatus = ref("");
      const logs = ref([]);
      const newAccountId = ref("");
      const newAccountPath = ref("");

      const config = computed(() => M.store.config || {});
      const accounts = computed(() => {
        const a = M.store.accounts;
        if (!a) return [];
        // 后端返回 { current, accounts: [ {name, dbPath, ...} ] }
        return Array.isArray(a.accounts) ? a.accounts : [];
      });
      const wx = computed(() => (M.store.status && M.store.status.wechat) || {});
      const svc = computed(() => (M.store.status && M.store.status.service) || {});
      const llm = ref({ baseUrl: "", apiKey: "", model: "" });

      const mcpSnippet = computed(() => {
        const cfg = (M.store.mcpConfig) || {};
        const mosu = cfg.mosu || {};
        return JSON.stringify({
          mcpServers: {
            mosu: {
              command: "python",
              args: mosu.args || ["core/mcp_server.py"],
              env: mosu.env || {},
            },
          },
        }, null, 2);
      });

      async function saveKeys() {
        busy.value = true;
        keyMsg.value = "";
        try {
          const d = await M.api.post("/api/key", {
            keyHex: keyHex.value.trim(),
            imgKeyHex: imgKeyHex.value.trim(),
          });
          if (d.error) throw new Error(d.error);
          keyOk.value = true;
          keyMsg.value = d.message || "已保存";
          await M.refreshConfig();
        } catch (e) {
          keyOk.value = false;
          keyMsg.value = "保存失败：" + e.message;
        } finally {
          busy.value = false;
        }
      }

      async function selftest() {
        busy.value = true;
        keyMsg.value = "";
        try {
          const d = await M.api.get("/api/key/selftest");
          if (d.error) throw new Error(d.error);
          keyOk.value = !!d.ok;
          keyMsg.value = d.message || (d.ok ? "密钥有效" : "密钥无效");
        } catch (e) {
          keyOk.value = false;
          keyMsg.value = "自检失败：" + e.message;
        } finally {
          busy.value = false;
        }
      }

      async function useAccount(id) {
        busy.value = true;
        try {
          const d = await M.api.post("/api/account", { action: "use", wxid: id });
          if (d.error) throw new Error(d.error);
          await M.refreshConfig();
          M.toast.ok("已切换到 " + id);
        } catch (e) {
          M.toast.err("切换失败：" + e.message);
        } finally {
          busy.value = false;
        }
      }

      async function removeAccount(id) {
        busy.value = true;
        try {
          const d = await M.api.post("/api/account", { action: "remove", wxid: id });
          if (d.error) throw new Error(d.error);
          await M.refreshConfig();
          M.toast.ok("已删除");
        } catch (e) {
          M.toast.err("删除失败：" + e.message);
        } finally {
          busy.value = false;
        }
      }

      async function addAccount() {
        busy.value = true;
        try {
          const d = await M.api.post("/api/account", {
            action: "add",
            wxid: newAccountId.value.trim(),
            dbPath: newAccountPath.value.trim(),
          });
          if (d.error) throw new Error(d.error);
          newAccountId.value = "";
          newAccountPath.value = "";
          await M.refreshConfig();
          M.toast.ok("已添加");
        } catch (e) {
          M.toast.err("添加失败：" + e.message);
        } finally {
          busy.value = false;
        }
      }

      async function startLogin() {
        busy.value = true;
        qrStatus.value = "正在获取二维码…";
        try {
          const d = await M.api.post("/api/wechat/login", {});
          if (d.error) throw new Error(d.error);
          // 二维码图片由服务端渲染成 PNG，前端从 /api/wechat/qr 取
          qrId.value = d.qrcode || "";
          qrSrc.value = "/api/wechat/qr?t=" + Date.now();
          qrStatus.value = "请用微信扫码";
          autoPoll();
        } catch (e) {
          qrStatus.value = "获取失败：" + e.message;
          M.toast.err("获取二维码失败：" + e.message);
        } finally {
          busy.value = false;
        }
      }

      let pollTimer = null;
      async function autoPoll() {
        for (let i = 0; i < 60; i++) {
          await new Promise((r) => setTimeout(r, 1500));
          const st = await pollOnce(true);
          if (st === true) return;
        }
      }

      async function pollOnce(silent) {
        try {
          const d = await M.api.get("/api/wechat/poll", { qrcode: qrId.value });
          if (d.logged_in || d.status === "ok" || d.confirmed) {
            qrStatus.value = "登录成功";
            qrSrc.value = "";
            await M.refreshStatus();
            if (!silent) M.toast.ok("登录成功");
            return true;
          }
          if (!silent) qrStatus.value = d.message || "等待扫码…";
          return false;
        } catch (e) {
          if (!silent) qrStatus.value = "检查失败：" + e.message;
          return false;
        }
      }

      async function wxAction(action) {
        busy.value = true;
        try {
          const d = await M.api.post("/api/wechat/action", { action });
          if (d.error) throw new Error(d.error);
          await M.refreshStatus();
          M.toast.ok(action === "start" ? "服务已启动" : "服务已停止");
        } catch (e) {
          M.toast.err("操作失败：" + e.message);
        } finally {
          busy.value = false;
        }
      }

      async function loadLogs() {
        try {
          const d = await M.api.get("/api/log", { n: 50 });
          // 后端返回 { events: [ {time, kind, text} ] }
          logs.value = (d.events || []).map((x) => x.text || "");
        } catch (e) { /* 忽略 */ }
      }

      async function loadMcp() {
        try {
          const d = await M.api.get("/api/mcp");
          mcpTools.value = d.tools || [];
          M.store.mcpConfig = d.config || null;
        } catch (e) { /* 忽略 */ }
      }

      async function mcpSelftest() {
        busy.value = true;
        mcpMsg.value = "";
        try {
          const d = await M.api.post("/api/mcp/selftest", {});
          if (d.error) throw new Error(d.error);
          mcpOk.value = !!d.ok;
          mcpMsg.value = d.message || (d.ok ? "自检通过" : "自检失败");
        } catch (e) {
          mcpOk.value = false;
          mcpMsg.value = "自检失败：" + e.message;
        } finally {
          busy.value = false;
        }
      }

      function copySnippet() {
        try {
          navigator.clipboard.writeText(mcpSnippet.value);
          M.toast.ok("已复制到剪贴板");
        } catch (e) {
          M.toast.err("复制失败，请手动选中");
        }
      }

      async function saveLlm() {
        busy.value = true;
        llmMsg.value = "";
        try {
          // apiKey 输入框回填的是掩码，留空表示"不改"，不能把真值抹掉
          const payload = Object.assign({}, llm.value);
          if (!payload.apiKey) delete payload.apiKey;
          const d = await M.api.post("/api/llm", payload);
          if (d.error) throw new Error(d.error);
          llmOk.value = true;
          llmMsg.value = "已保存";
          await M.refreshConfig();
        } catch (e) {
          llmOk.value = false;
          llmMsg.value = "保存失败：" + e.message;
        } finally {
          busy.value = false;
        }
      }

      onMounted(async () => {
        if (props.route && props.route.includes(".")) {
          const t = props.route.split(".")[1];
          if (TABS.some((x) => x.id === t)) tab.value = t;
        }
        await M.refreshConfig();
        if (M.store.llm) {
          llm.value = {
            baseUrl: M.store.llm.baseUrl || "",
            // 后端返回的 apiKey 是掩码 "***"，不要回填到输入框（会覆盖真值）
            apiKey: "",
            model: M.store.llm.model || "",
          };
        }
        await loadMcp();
        if (tab.value === "channel") loadLogs();
      });

      return {
        tabs: TABS, tab, busy, config, accounts, wx, svc, llm, logs,
        keyHex, imgKeyHex, keyMsg, keyOk, mcpMsg, mcpOk, llmMsg, llmOk,
        mcpTools, mcpSnippet, qrSrc, qrId, qrStatus, newAccountId, newAccountPath,
        saveKeys, selftest, useAccount, removeAccount, addAccount,
        startLogin, pollOnce, wxAction, loadLogs, loadMcp, mcpSelftest,
        copySnippet, saveLlm, M,
        fmtTime: M.fmtTime,
      };
    },
  };

  global.MosuV2 = global.MosuV2 || {};
  global.MosuV2.Settings = Settings;
})(window);
