/* ==========================================================================
   页面 · MCP 服务（工具清单 / 接入配置 / 一键自检）
   对应 CLI：mosu mcp serve|selftest|config

   MCP server 本体是 AI 客户端（Claude Desktop / WorkBuddy 等）按需拉起的
   stdio 进程，GUI 不常驻它——所以本页没有「启停」，只有三样：
   有哪些工具、怎么接进去、测一下好不好用。
   ========================================================================== */
(function (global) {
  "use strict";
  const M = global.Mosu;

  const Mcp = {
    template: `
    <div class="page">
      <div class="grid grid-3 rise">
        <div class="tile">
          <div class="tile-head">可用工具</div>
          <div class="tile-value">{{ count }}</div>
          <div class="tile-foot">AI 客户端可直接调用</div>
        </div>
        <div class="tile">
          <div class="tile-head">协议</div>
          <div class="tile-value" style="font-size:20px">stdio</div>
          <div class="tile-foot">AI 客户端拉起，不占端口</div>
        </div>
        <div class="tile">
          <div class="tile-head">数据来源</div>
          <div class="tile-value" style="font-size:20px">
            {{ ready ? '本机已就绪' : '未配置' }}
          </div>
          <div class="tile-foot">与 GUI 同一份解密数据</div>
        </div>
      </div>

      <div class="card" style="margin-top:16px">
        <div class="card-title">工具清单</div>
        <div class="card-sub">
          AI 客户端连上后，这些话它都能说——查状态、列会话、读消息、搜关键词、
          统计、看朋友圈、生成画像与年报、导出、甚至「看」聊天里的图。
        </div>
        <div class="kv" style="margin-top:12px">
          <template v-for="t in tools" :key="t.name">
            <dt style="min-width:150px"><code>{{ t.name }}</code></dt>
            <dd>{{ t.description }}</dd>
          </template>
        </div>
      </div>

      <div class="card" style="margin-top:16px">
        <div class="card-title">接入配置</div>
        <div class="card-sub">
          把下面这段加进 AI 客户端的 MCP 配置文件（如 WorkBuddy 的
          <code>~/.workbuddy/mcp.json</code>、Claude Desktop 的
          <code>claude_desktop_config.json</code>）的 <code>mcpServers</code> 里，
          然后在客户端里点「信任」。路径已按本机填好，直接复制即可。
        </div>
        <pre class="md-pre" style="max-height:none">{{ configText }}</pre>
        <button class="btn btn-primary" style="margin-top:12px" @click="copyConfig">
          {{ copied ? '已复制 ✓' : '复制配置' }}
        </button>
      </div>

      <div class="card" style="margin-top:16px">
        <div class="card-title">一键自检</div>
        <div class="card-sub">
          不启动服务，直接把每个工具跑一遍（真实数据冒烟）。大库搜索会慢，
          最多等 150 秒。
        </div>
        <div class="row" style="margin-top:12px">
          <button class="btn btn-primary" @click="selftest" :disabled="testing">
            <span v-if="testing" class="spinner"></span>
            {{ testing ? '自检中…' : '开始自检' }}
          </button>
        </div>
        <div v-if="testErr" class="tiny" style="color:var(--danger);margin-top:10px">
          {{ testErr }}
        </div>
        <pre v-if="testOut" class="md-pre" style="margin-top:12px">{{ testOut }}</pre>
      </div>

      <div class="card" style="margin-top:16px">
        <div class="card-title">大白话解释</div>
        <ul class="tiny muted" style="padding-left:18px;line-height:1.9">
          <li><strong>MCP 是什么</strong>：一个让 AI 客户端「伸手够到本地工具」的标准
              插座。墨溯插上它，AI 就能直接查你的聊天数据，而不只是听你口头描述</li>
          <li><strong>数据出境吗</strong>：工具在本机跑，回传给 AI 的只有它问到的那部分；
              密钥和数据库都不离开本机</li>
          <li><strong>不想用了</strong>：把配置段删掉即断开，墨溯这边无任何残留</li>
        </ul>
      </div>
    </div>`,
    data() {
      return {
        tools: [], count: 0, config: {},
        copied: false, testing: false, testOut: "", testErr: "",
      };
    },
    computed: {
      ready() {
        return !!(M.store.status && M.store.status.data
                  && M.store.status.data.ready);
      },
      configText() {
        return JSON.stringify(this.config, null, 2);
      },
    },
    methods: {
      async load() {
        try {
          const d = await M.api.get("/api/mcp");
          this.tools = d.tools || [];
          this.count = d.count || 0;
          this.config = d.config || {};
        } catch (e) {
          M.toast.err("加载 MCP 信息失败：" + e.message);
        }
      },
      async copyConfig() {
        const text = this.configText;
        try {
          await navigator.clipboard.writeText(text);
        } catch (e) {
          // 剪贴板 API 不可用（旧浏览器/权限）：退化为选中文本让用户手动复制
          const ta = document.createElement("textarea");
          ta.value = text;
          document.body.appendChild(ta);
          ta.select();
          try { document.execCommand("copy"); } catch (_) { /* 用户手动复制 */ }
          ta.remove();
        }
        this.copied = true;
        setTimeout(() => { this.copied = false; }, 2000);
      },
      async selftest() {
        this.testing = true; this.testOut = ""; this.testErr = "";
        try {
          const d = await M.api.post("/api/mcp/selftest", {});
          this.testOut = d.output || "";
          if (!d.ok) this.testErr = d.error || "自检未通过";
          else M.toast.ok("自检通过");
        } catch (e) {
          this.testErr = e.message || String(e);
        } finally { this.testing = false; }
      },
    },
    async mounted() { await this.load(); },
  };

  global.MosuViews = global.MosuViews || {};
  Object.assign(global.MosuViews, { Mcp });
})(window);
