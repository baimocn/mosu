/* ==========================================================================
   墨溯 v2 · 跨页通信与全局桥
   首页 → 聊天 → 分享 之间的跳转需要带状态。零构建单页没有路由库，
   这里用一个极小的共享对象承载"待办"状态。
   ========================================================================== */
(function (global) {
  "use strict";
  const M = global.Mosu;
  const V = (global.MosuV2 = global.MosuV2 || {});

  /* 待传递状态 */
  V.pendingSession = null;
  V.pendingSearch = null;
  V.pendingExportSession = null;

  V.selectSession = function (sessionId) {
    V.pendingSession = sessionId;
    // 聊天页可能尚未挂载，用事件兜底
    global.dispatchEvent(new CustomEvent("mosu:select-session", { detail: sessionId }));
  };

  V.setSearch = function (kw) {
    V.pendingSearch = kw;
    global.dispatchEvent(new CustomEvent("mosu:set-search", { detail: kw }));
  };

  V.setExportSession = function (sessionId) {
    V.pendingExportSession = sessionId;
    global.dispatchEvent(new CustomEvent("mosu:set-export-session", { detail: sessionId }));
  };

  /* 聊天页监听事件，兼容"页面已挂载"的场景 */
  global.addEventListener("mosu:select-session", function (e) {
    V.pendingSession = e.detail;
    const chat = V.Chat;
    if (chat && chat.__instance) {
      const inst = chat.__instance;
      const s = M.store.sessions.find((x) => x.sessionId === e.detail);
      if (s && inst.pick) inst.pick(s);
    }
  });

  global.addEventListener("mosu:set-search", function (e) {
    V.pendingSearch = e.detail;
    const chat = V.Chat;
    if (chat && chat.__instance) {
      const inst = chat.__instance;
      if (inst.kw !== undefined) inst.kw = e.detail;
      if (inst.runSearch) inst.runSearch();
    }
  });
})(window);
