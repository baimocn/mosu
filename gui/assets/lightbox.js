/* ==========================================================================
   通用图片查看器（灯箱）
   三段式加载：先用缩略图立刻占位（不白屏）→ 后台换原图 → 失败给重试。
   本地图片（/api/media?md5=）与远程图片（朋友圈 CDN）都能用：
   只要传 fullSrc；没有 fullSrc 时退化为直接显示 src。
   ========================================================================== */
(function (global) {
  "use strict";
  const M = global.Mosu;
  const ico = (n) => (global.MosuIcons && global.MosuIcons[n]) || "";

  const Lightbox = {
    name: "Lightbox",
    props: {
      /** { thumb: 缩略图地址, full: 原图地址, alt: 说明 } */
      item: { type: Object, default: null },
      /** 兼容旧调用：直接传字符串 */
      src: { type: String, default: "" },
    },
    data() {
      return {
        stage: "loading",   // loading | ok | error | remote-missing
        fullSrc: "",
        reloadKey: 0,
        zoom: 1,
        ox: 0, oy: 0,
        dragging: false,
      };
    },
    computed: {
      thumbSrc() {
        if (!this.item) return "";
        return this.item.thumb || this.item.full || "";
      },
      displaySrc() {
        // 原图没到位时显示缩略图，避免白屏；到位后切原图
        if (this.stage === "ok") return this.fullSrc;
        return this.thumbSrc;
      },
      hint() {
        if (this.stage === "error") return "原图加载失败";
        if (this.stage === "remote-missing") return "本地没有这张图，且远程地址已失效";
        return "";
      },
    },
    watch: {
      item: { immediate: true, handler() { this.reset(); } },
      src(v) { if (v && !this.item) { this.reset(); } },
    },
    mounted() {
      window.addEventListener("keydown", this.onKey);
    },
    beforeUnmount() {
      window.removeEventListener("keydown", this.onKey);
    },
    methods: {
      ico,
      reset() {
        this.zoom = 1; this.ox = 0; this.oy = 0;
        this.reloadKey++;
        const full = (this.item && this.item.full) || this.src || "";
        if (!full) {
          this.stage = "remote-missing";
          this.fullSrc = "";
          return;
        }
        this.fullSrc = full;
        this.stage = "loading";
      },
      onKey(e) {
        if (!this.item && !this.src) return;
        if (e.key === "Escape") this.close();
      },
      close() {
        if (this.item) this.$emit("close");
        else this.$emit("close");
      },
      onLoad() { this.stage = "ok"; },
      onErr() {
        // 远程图先试一次缩略图（朋友圈 CDN 偶发大图 403）
        if (this.stage === "loading" && this.thumbSrc &&
            this.fullSrc !== this.thumbSrc) {
          this.stage = "error";
        } else {
          this.stage = "error";
        }
      },
      retry() { this.reloadKey++; this.stage = "loading"; },
      wheel(e) {
        e.preventDefault();
        const next = this.zoom * (e.deltaY < 0 ? 1.15 : 1 / 1.15);
        this.zoom = Math.min(4, Math.max(1, next));
        if (this.zoom === 1) { this.ox = 0; this.oy = 0; }
      },
      onDown(e) {
        if (this.zoom <= 1) return;
        this.dragging = true;
        this.sx = e.clientX; this.sy = e.clientY;
        this.bx = this.ox; this.by = this.oy;
      },
      onMove(e) {
        if (!this.dragging) return;
        this.ox = this.bx + (e.clientX - this.sx);
        this.oy = this.by + (e.clientY - this.sy);
      },
      onUp() { this.dragging = false; },
      imgStyle() {
        return {
          transform: `translate(${this.ox}px, ${this.oy}px) scale(${this.zoom})`,
          cursor: this.zoom > 1 ? (this.dragging ? "grabbing" : "grab") : "zoom-in",
        };
      },
    },
    template: `
    <div class="lightbox" @click.self="close"
         @wheel.prevent="wheel" @mousedown="onDown"
         @mousemove="onMove" @mouseup="onUp" @mouseleave="onUp">
      <button class="lightbox-close" @click="close" title="关闭（Esc）"
              v-html="ico('close')"></button>

      <div class="lightbox-stage" @click="zoom < 1 ? 0 : (zoom = 1, ox = 0, oy = 0)">
        <img v-if="displaySrc" :key="reloadKey" :src="displaySrc"
             :style="imgStyle" class="lightbox-img"
             :class="{'is-loading': stage==='loading'}"
             draggable="false" @load="onLoad" @error="onErr" />

        <div v-if="stage==='loading'" class="lightbox-mask">
          <div class="spinner spinner-lg"></div>
          <div class="tiny">正在加载原图…</div>
        </div>

        <div v-else-if="stage==='error' || stage==='remote-missing'" class="lightbox-mask">
          <div class="lightbox-err-icon">🖼</div>
          <div class="lightbox-err-msg">{{ hint }}</div>
          <div class="tiny muted">
            <template v-if="stage==='remote-missing'">
              微信 CDN 的临时地址已过期，本机媒体库里也没有对应文件。
            </template>
            <template v-else>
              网络或本地解密可能瞬时失败，可重试。
            </template>
          </div>
          <button class="btn btn-sm" style="margin-top:10px" @click="retry"
                  v-if="stage==='error'"><span class="nav-icon"
                  style="width:14px;height:14px" v-html="ico('retry')"></span>重新加载</button>
        </div>
      </div>

      <div class="lightbox-bar" v-if="stage==='ok'">
        <span class="tiny muted">{{ item && item.note ? item.note : '滚轮缩放 · 拖拽移动 · Esc 关闭' }}</span>
        <span class="spacer" style="flex:1"></span>
        <button class="btn btn-sm" @click="zoom = Math.max(1, zoom / 1.25); ox = 0; oy = 0">－</button>
        <span class="tiny muted" style="min-width:44px;text-align:center">{{ Math.round(zoom * 100) }}%</span>
        <button class="btn btn-sm" @click="zoom = Math.min(4, zoom * 1.25)">＋</button>
        <button class="btn btn-sm" @click="zoom = 1; ox = 0; oy = 0">适应</button>
      </div>
    </div>`,
  };

  /** 聊天/朋友圈图片地址构造：本地优先，远程兜底 */
  function mediaUrl(md5, thumb) {
    if (!md5) return "";
    return "/api/media?md5=" + encodeURIComponent(md5) + (thumb ? "&thumb=1" : "");
  }

  global.MosuViews = global.MosuViews || {};
  global.MosuViews.Lightbox = Lightbox;
  global.Mosu.mediaUrl = mediaUrl;
})(window);