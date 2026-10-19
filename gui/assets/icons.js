/* ==========================================================================
   墨溯 · 导航图标
   一套手绘线性图标，替换 emoji。统一规则：
   · 24×24 viewBox，1.7px 描边，圆角端点与连接
   · currentColor 跟随主题（浅色深色都清晰），选中态自动变主色
   · 图形语义按模块重新设计，不是随便挑个近似 emoji
   ========================================================================== */
(function (global) {
  "use strict";

  const S = (d, extra) =>
    `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"
      aria-hidden="true">${d}${extra || ""}</svg>`;

  const ICONS = {
    /* 控制台：终端窗口 + 提示符（对话调试入口） */
    console: S(`<rect x="2.5" y="4" width="19" height="16" rx="3"/>
                <path d="M6.5 9.5l3 2.5-3 2.5"/>
                <path d="M12 15h5"/>`),

    /* 数据浏览：对话气泡 + 三点（聊天记录本体） */
    browse: S(`<path d="M20.5 12.5c0 3.9-3.8 7-8.5 7-.9 0-1.8-.1-2.6-.3L4 21l1-3.6c-1.4-1.3-2.2-3.1-2.2-4.9
                        C2.8 8.6 6.6 5.5 11.3 5.5s9.2 3.1 9.2 7z"/>
              <circle cx="7.6" cy="12.4" r=".9" fill="currentColor" stroke="none"/>
              <circle cx="11.3" cy="12.4" r=".9" fill="currentColor" stroke="none"/>
              <circle cx="15" cy="12.4" r=".9" fill="currentColor" stroke="none"/>`),

    /* 全库搜索：放大镜（带一道扫描线，暗示「全量」） */
    search: S(`<circle cx="10.8" cy="10.8" r="6.3"/>
               <path d="M15.4 15.4l4.1 4.1"/>
               <path d="M8 11.4h5.6" stroke-opacity=".45"/>`),

    /* 数据统计：柱状 + 上行折线 */
    stats: S(`<path d="M4 20h16"/>
             <path d="M7.5 20v-5"/>
             <path d="M12 20V8.5"/>
             <path d="M16.5 20v-8"/>
             <path d="M5.6 9.6l4.2-3.4 3.6 2.2 4.4-4.1" stroke-opacity=".55"/>`),

    /* 画像：人像轮廓 + 侧边刻度（由数据勾勒出的一个人） */
    profile: S(`<circle cx="11" cy="8" r="3.6"/>
                <path d="M4.6 20.2c.5-3.9 3.1-6 6.4-6s5.9 2.1 6.4 6"/>
                <path d="M18.6 7.5v5.5"/>
                <path d="M21 9.2v2.2"/>`),

    /* 年报：日历（带已填写的一天） */
    report: S(`<rect x="3.2" y="5" width="17.6" height="16" rx="3"/>
               <path d="M3.2 10h17.6"/>
               <path d="M8 3.2V6.6"/>
               <path d="M16 3.2V6.6"/>
               <path d="M7.4 14h2.4"/>
               <path d="M13.4 14h3.2" stroke-opacity=".45"/>`),

    /* 联系人：两个人 */
    contacts: S(`<circle cx="9.4" cy="8.6" r="3.3"/>
                 <path d="M3.4 19.6c.5-3.5 2.9-5.5 6-5.5s5.5 2 6 5.5"/>
                 <path d="M16.2 6.1a3.3 3.3 0 010 5.6" stroke-opacity=".55"/>
                 <path d="M18.2 14.4c1.7.7 2.8 2.4 3 4.7" stroke-opacity=".55"/>`),

    /* 朋友圈：相片叠放（图片流） */
    moments: S(`<rect x="3" y="6.4" width="18" height="13.2" rx="2.6"/>
                <circle cx="8.4" cy="11" r="1.6"/>
                <path d="M3.6 17.4l4.6-4.2 3.4 3 3-2.6 5.8 5"/>
                <path d="M6.6 4.2h10.8" stroke-opacity=".45"/>`),

    /* 导出：文件落入托盘 */
    export: S(`<path d="M12 3.6v9.8"/>
              <path d="M8.2 9.8L12 13.6l3.8-3.8"/>
              <path d="M4.6 15.4v3a2 2 0 002 2h10.8a2 2 0 002-2v-3"/>`),

    /* 设置：齿轮 */
    settings: S(`<circle cx="12" cy="12" r="3.1"/>
                 <path d="M12 3.2l1.1 2.1 2.3-.6.6 2.3 2.1 1.1-1.2 2.1 1.2 2.1-2.1 1.1-.6 2.3
                          -2.3-.6L12 20.8l-1.1-2.1-2.3.6-.6-2.3L5.9 15.9l1.2-2.1-1.2-2.1 2.1-1.1
                          .6-2.3 2.3.6z"/>`),
  };

  global.MosuIcons = ICONS;
  global.Mosu = global.Mosu || {};
  global.Mosu.icon = (name) => ICONS[name] || "";
})(window);