/* ==========================================================================
   页面 · 微信通道（扫码登录 / 连接状态 / 常驻管理）
   对应 CLI：mosu wechat login|status|install|uninstall|task

   交互主线：取码（服务端渲染 PNG）→ 轮询 → confirmed 落凭证。
   轮询状态机：wait → scanned → confirmed；expired 自动换码（最多 3 次）；
   need_verifycode 时请用户补微信上显示的数字。全程前端零二维码库、
   零外网请求（图由本机 qrcode 包渲染）。
   ========================================================================== */
(function (global) {
  "use strict";
  const M = global.Mosu;

  const WeChat = {
    template: `
    <div class="page">
      <div class="grid grid-4 rise">
        <div class="tile">
          <div class="tile-head">连接状态</div>
          <div class="tile-value" style="font-size:20px">
            {{ wx.logged_in ? '已连接' : '未连接' }}
          </div>
          <div class="tile-foot">{{ wx.logged_in ? '分身可收可发' : '扫码后即可收发' }}</div>
        </div>
        <div class="tile">
          <div class="tile-head">常驻任务</div>
          <div class="tile-value" style="font-size:20px">
            {{ task.installed ? '已安装' : '未安装' }}
          </div>
          <div class="tile-foot">
            {{ task.installed ? (task.channel + (task.state ? ' · ' + task.state : '')) : '登录后不会自启' }}
          </div>
        </div>
        <div class="tile">
          <div class="tile-head">消息游标</div>
          <div class="tile-value" style="font-size:20px">{{ wx.cursor || '—' }}</div>
          <div class="tile-foot">断点续收的位置</div>
        </div>
        <div class="tile">
          <div class="tile-head">服务地址</div>
          <div class="tile-value" style="font-size:13px;word-break:break-all">
            {{ wx.base_url || '—' }}
          </div>
          <div class="tile-foot">微信官方 iLink</div>
        </div>
      </div>

      <div class="grid grid-2" style="margin-top:16px">
        <!-- 登录 / 绑定 -->
        <div class="card">
          <div class="card-title">扫码绑定</div>
          <div class="card-sub">
            用本人微信扫描二维码，把机器人绑定到你的微信上。一个机器人同时只能被一个
            消费者绑定；绑定后别人给你发消息，墨溯会用你的风格代回。
          </div>

          <div v-if="wx.logged_in" style="margin-top:12px">
            <div class="notice" style="margin-bottom:12px">
              当前已绑定。解绑后分身停止收发；重新扫码即可再绑定。
            </div>
            <button class="btn" @click="logout" :disabled="busy">
              断开连接
            </button>
          </div>

          <div v-else-if="phase==='idle'" style="margin-top:12px">
            <button class="btn btn-primary" @click="startLogin" :disabled="busy">
              <span v-if="busy" class="spinner"></span>打开微信扫码
            </button>
            <div class="tiny muted" style="margin-top:8px">
              二维码有效期 5 分钟，过期会自动换（最多 3 次）。
            </div>
          </div>

          <div v-else-if="phase==='qr'" style="margin-top:12px;text-align:center">
            <img v-if="qrReady" :src="qrUrl" alt="登录二维码"
                 style="width:220px;height:220px;border:1px solid var(--border);border-radius:12px;background:#fff" />
            <div v-else class="notice" style="text-align:left">
              本机缺少 qrcode 库，画不出图。请复制下面链接，在浏览器打开后按提示操作：
              <div style="margin-top:6px;word-break:break-all">
                <a :href="qrLink" target="_blank" rel="noopener">{{ qrLink }}</a>
              </div>
            </div>
            <div class="tiny muted" style="margin-top:10px">
              {{ pollText }} · 剩余 {{ countdown }} 秒
            </div>
            <div v-if="needCode" class="field" style="margin-top:12px;text-align:left">
              <label class="field-label">微信上显示的数字</label>
              <div class="row">
                <input class="input" style="width:140px" v-model="verifyCode"
                       placeholder="数字验证码" maxlength="8" />
                <button class="btn btn-sm" @click="submitVerify" :disabled="busy">
                  提交
                </button>
              </div>
            </div>
            <button class="btn btn-sm" style="margin-top:12px" @click="cancelLogin">
              取消
            </button>
          </div>

          <div v-else-if="phase==='done'" style="margin-top:12px">
            <div class="notice">✅ 绑定成功，分身已可收发。</div>
            <button class="btn btn-sm" style="margin-top:10px" @click="phase='idle'">
              知道了
            </button>
          </div>

          <div v-if="err" class="tiny" style="color:var(--danger);margin-top:10px">
            {{ err }}
          </div>
        </div>

        <!-- 常驻管理 -->
        <div class="card">
          <div class="card-title">常驻管理</div>
          <div class="card-sub">
            安装后：开机自启 + 崩溃自愈（计划任务优先，被策略禁时自动降级 schtasks /
            启动文件夹守护）。不安装则每次要手动运行。
          </div>
          <div class="row" style="margin-top:12px">
            <button class="btn btn-primary" @click="install"
                    :disabled="busy || task.installed">
              安装常驻
            </button>
            <button class="btn" @click="uninstall" :disabled="busy || !task.installed">
              卸载常驻
            </button>
            <div class="spacer"></div>
            <button class="btn btn-sm" @click="loadTask" :disabled="busy">刷新状态</button>
          </div>
          <div class="kv" style="margin-top:14px">
            <dt>任务名</dt><dd>{{ task.name || '—' }}</dd>
            <dt>通道</dt><dd>{{ task.channel || '未安装' }}</dd>
            <dt>状态</dt><dd>{{ task.state || '—' }}</dd>
          </div>
        </div>
      </div>

      <div class="card" style="margin-top:16px">
        <div class="card-title">使用说明</div>
        <ul class="tiny muted" style="padding-left:18px;line-height:1.9">
          <li>绑定成功后运行常驻服务（安装常驻即自动覆盖），分身才能在别人发消息时回话</li>
          <li>在微信里给机器人发 <code>/倾听</code> <code>/分析</code> <code>/安慰</code>
              <code>/模仿</code> 可切换人格；首次回复后会提示一次</li>
          <li>分身回复走你配置的大模型 + 你历史发言的风格检索，全部规则校验通过才发出</li>
          <li>解绑只清本机凭证，不会动微信上的任何数据</li>
        </ul>
      </div>
    </div>`,
    data() {
      return {
        phase: "idle",           // idle | qr | done
        qrcode: "", qrLink: "", qrReady: false,
        pollText: "等待扫描", countdown: 300,
        needCode: false, verifyCode: "",
        busy: false, err: "",
        task: { name: "", installed: false, channel: "", state: "" },
        _timer: null, _tick: null, _refresh: 0,
      };
    },
    computed: {
      wx() {
        const w = (M.store.status && M.store.status.wechat) || {};
        return { logged_in: !!w.logged_in, base_url: w.base_url || "",
                 cursor: w.cursor || false };
      },
      qrUrl() {
        // 服务端把 URL 渲染成 ~/.mosu/wechat-login.png；加时间戳破缓存
        return "/api/wechat/qr?t=" + Date.now();
      },
    },
    methods: {
      async loadTask() {
        try {
          const d = await M.api.get("/api/wechat/task");
          this.task = d || this.task;
        } catch (e) { /* 忽略：任务状态不影响主流程 */ }
      },
      async startLogin() {
        this.busy = true; this.err = "";
        try {
          const d = await M.api.post("/api/wechat/login", {});
          this.qrcode = d.qrcode || "";
          this.qrLink = d.url || "";
          this.qrReady = !!d.qrReady;
          this.countdown = d.ttl || 300;
          this.phase = "qr";
          this.needCode = false;
          this.verifyCode = "";
          this._refresh = 0;
          this.startPolling();
        } catch (e) {
          this.err = e.message || String(e);
        } finally { this.busy = false; }
      },
      startPolling() {
        this.stopPolling();
        this._tick = setInterval(() => {
          if (this.countdown > 0) this.countdown -= 1;
        }, 1000);
        this._timer = setInterval(() => this.poll(), 2500);
        this.poll();
      },
      stopPolling() {
        if (this._timer) { clearInterval(this._timer); this._timer = null; }
        if (this._tick) { clearInterval(this._tick); this._tick = null; }
      },
      async poll() {
        if (!this.qrcode) return;
        try {
          const d = await M.api.get("/api/wechat/poll", { qrcode: this.qrcode });
          const st = d.status || "";
          if (st === "confirmed") {
            this.stopPolling();
            this.phase = "done";
            await M.refreshStatus();
            if (!d.saved) this.err = d.error || "凭证保存失败";
            return;
          }
          if (st === "expired") {
            this._refresh += 1;
            if (this._refresh > 3) {
              this.stopPolling();
              this.err = "二维码多次失效，请稍后重试";
              this.phase = "idle";
              return;
            }
            this.pollText = "二维码已过期，正在更换…";
            await this.startLogin();
            return;
          }
          if (st === "need_verifycode" || st === "verify_code_blocked") {
            this.needCode = true;
            this.pollText = "需要验证码";
            return;
          }
          this.pollText = { wait: "等待扫描", scanned: "已扫描，请在手机上确认" }[st]
                           || ("状态：" + st);
        } catch (e) {
          this.pollText = "轮询失败：" + (e.message || e);
        }
      },
      async submitVerify() {
        if (!this.verifyCode.trim()) return;
        this.needCode = false;
        this.pollText = "验证码已提交，等待确认…";
        try {
          const d = await M.api.get("/api/wechat/poll", {
            qrcode: this.qrcode, verify: this.verifyCode.trim(),
          });
          if ((d.status || "") === "confirmed") {
            this.stopPolling();
            this.phase = "done";
            await M.refreshStatus();
            if (!d.saved) this.err = d.error || "凭证保存失败";
          }
        } catch (e) {
          this.err = e.message || String(e);
        }
      },
      cancelLogin() {
        this.stopPolling();
        this.phase = "idle";
        this.qrcode = "";
      },
      async logout() {
        this.busy = true; this.err = "";
        try {
          await M.api.post("/api/wechat/action", { action: "logout" });
          await M.refreshStatus();
          M.toast.ok("已断开连接");
        } catch (e) {
          this.err = e.message || String(e);
        } finally { this.busy = false; }
      },
      async install() {
        this.busy = true; this.err = "";
        try {
          await M.api.post("/api/wechat/action", { action: "install" });
          await this.loadTask();
          M.toast.ok("常驻已安装");
        } catch (e) {
          this.err = e.message || String(e);
        } finally { this.busy = false; }
      },
      async uninstall() {
        this.busy = true; this.err = "";
        try {
          await M.api.post("/api/wechat/action", { action: "uninstall" });
          await this.loadTask();
          M.toast.ok("常驻已卸载");
        } catch (e) {
          this.err = e.message || String(e);
        } finally { this.busy = false; }
      },
    },
    async mounted() {
      await this.loadTask();
    },
    beforeUnmount() { this.stopPolling(); },
  };

  global.MosuViews = global.MosuViews || {};
  Object.assign(global.MosuViews, { WeChat });
})(window);
