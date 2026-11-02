/* ==========================================================================
   墨溯 v2 · 分支树（多代理并行编排的可视化）
   --------------------------------------------------------------------------
   对标 Lancer 的 `process-tree.tsx` + `process-curve-routes.ts`：
   泳道（lane）分列 + 语义锚点光线。

   抄的核心纪律（两条，都是 Lancer 最值钱的部分）：

   1. **颜色按泳道分配一次，全链路继承**
      随机色每次刷新都变，用户没法把颜色和某个子代理对上；
      分支增删后洗牌更糟——同一条线换个颜色就没人认得了。
      所以颜色由后端算好下发（branch.laneColor），前端不自己配色。

   2. **锚点缺失不乱飞**
      Lancer 的 `readCurveAnchor` 读不到锚点时返回 null，
      上层直接 `if (!a || !b) return;` —— **放弃发射这条线**，
      绝不把线落到邻近节点上。
      原注释原话：a clipped anchor cannot be replaced by a nearby node。
      这里照抄：找不到锚点就少画一条线，不画错线。
      少画一条线用户看不出来，画错线用户会以为是真关系。

   与 Lancer 的差异（有意为之）：
     - Lancer 每帧重算（实时飞行动画），墨溯是**静态布局**，
       渲染后算一次 + 窗口 resize 时重算即可
     - Lancer 用 React + SVG 库，墨溯是**零构建原生 JS**，
       所以曲线用原生 SVG path 手写贝塞尔
   ========================================================================== */
(function (global) {
  "use strict";
  const { ref, computed, onMounted, onBeforeUnmount, nextTick, watch } = Vue;

  // ---------------------------------------------------------------- 颜色
  // 泳道调色板与后端 core/agent_team.py 的 LANE_COLORS 一致。
  // 后端已经在 branch.laneColor 里给了最终颜色，这里只做兜底。
  const FALLBACK_COLORS = [
    "#3b82f6", "#a855f7", "#06b6d4", "#f59e0b",
    "#ec4899", "#10b981", "#8b5cf6", "#ef4444",
  ];

  function laneFallback(lane) {
    if (lane === null || lane === undefined || lane < 0) return FALLBACK_COLORS[0];
    return FALLBACK_COLORS[lane % FALLBACK_COLORS.length];
  }

  // 虚拟根节点的 id。
  //
  // 为什么要有虚拟根：只有一层分支时（全是顶层），父子边一条都没有，
  // 「树」会退化成三个并列卡片——**拓扑关系完全看不出来**。
  // 加一个代表主代理的虚拟根，让每个顶层分支都有一条从根出发的线，
  // 一眼就能看出「主代理并行派了 N 个子代理」。
  const ROOT_ID = "root";

  // ---------------------------------------------------------------- 曲线
  /**
   * 三次贝塞尔路径。控制点沿水平方向外推，
   * 让父子之间的连线呈「甩出去再收回」的弧线，而不是折线。
   */
  function cubicPath(a, b) {
    const dx = Math.max(24, Math.abs(b.x - a.x) * 0.45);
    const c1 = { x: a.x + dx, y: a.y };
    const c2 = { x: b.x - dx, y: b.y };
    return `M ${a.x} ${a.y} C ${c1.x} ${c1.y}, ${c2.x} ${c2.y}, ${b.x} ${b.y}`;
  }

  // ---------------------------------------------------------------- 组件
  const BranchTree = {
    name: "BranchTree",
    props: {
      // 分支数组（后端 Branch.to_dict() 的形状）
      branches: { type: Array, default: () => [] },
    },
    setup(props) {
      const rootEl = ref(null);
      const edges = ref([]);
      const semEdges = ref([]);
      const semHover = ref(null);
      let raf = 0;
      let loop = 0;

      // 把分支树摊平成「节点 + 边」两份列表。
      // 后端 to_dict 已经是嵌套的 children，这里递归展平。
      function flatten(list, parentId, depth) {
        const nodes = [];
        const links = [];
        (list || []).forEach((b) => {
          if (!b) return;
          const node = {
            id: b.id,
            goal: b.goal || "",
            status: b.status || "queued",
            receipt: b.receipt || "reported",
            // 结算状态（迭代 11）：
            //   settled      —— 子树是否收敛（程序判定：送达 + 终态）
            //   accepted     —— 父级**显式表态**采信（模型写 mosu-accepted）
            //   consumedBy   —— 返回被谁消费了（顶层是主代理，子层是直接父）
            // 三者是不同的事，别混成一个「已核对」。
            settled: b.settled === undefined ? false : !!b.settled,
            accepted: b.accepted === undefined ? false : !!b.accepted,
            // acceptedBy 必须一起传下来：徽标的 title 要区分
            // 「主代理采信」和「直接父采信」——
            // 只传 accepted 的话这里恒为 undefined，title 一律显示「直接父」。
            acceptedBy: b.acceptedBy || "",
            acceptReason: b.acceptReason || "",
            consumedBy: b.consumedBy || "",
            report: b.report || "",
            error: b.error || "",
            steps: b.steps || [],
            cost: b.cost || 0,
            depth: b.depth === undefined ? depth : b.depth,
            lane: b.lane === undefined ? 0 : b.lane,
            // 颜色优先用后端给的；后端没给（老落盘记录）才走兜底
            color: b.laneColor || laneFallback(b.lane),
            parentId: b.parentId || parentId || null,
            // 跨分支语义关联（迭代 12）：后端挂好的，摊平成边时要靠它
            semanticLinks: b.semanticLinks || [],
          };
          nodes.push(node);
          if (node.parentId) {
            links.push({ from: node.parentId, to: node.id, color: node.color });
          } else {
            // 顶层分支：从虚拟根出发。
            // 线用子分支自己的泳道色 → 一眼看出「根派出去的每路是哪个颜色」。
            links.push({ from: ROOT_ID, to: node.id, color: node.color });
          }
          const kids = flatten(b.children || [], b.id, depth + 1);
          nodes.push(...kids.nodes);
          links.push(...kids.links);
        });
        return { nodes, links };
      };

      const flat = computed(() => flatten(props.branches, null, 1));

      // ---------------------------------------------------------------- 语义边
      /**
       * 跨分支的语义关联（迭代 12）。
       *
       * 后端在每个分支上挂了 `semanticLinks`（两端都挂一份），
       * 这里把它摊平成「边」列表并**去重**——同一对分支在后端只算一条，
       * 但两端各挂了一份，直接遍历会画两条重合的线。
       *
       * 为什么语义边要单独一层、单独一种样式：
       *   树线（实线）说的是「谁派的谁」——编排关系；
       *   语义线（虚线）说的是「这两个分支查的是同一件事」——内容关系。
       *   两者混在一层里，用户会分不清。
       */
      const semanticEdges = computed(() => {
        const seen = new Set();
        const out = [];
        for (const n of flat.value.nodes) {
          const links = n.semanticLinks || [];
          for (const lk of links) {
            if (!lk || !lk.from || !lk.to) continue;
            const key = lk.from < lk.to
              ? lk.from + "->" + lk.to
              : lk.to + "->" + lk.from;
            if (seen.has(key)) continue;
            seen.add(key);
            out.push({
              id: key,
              from: lk.from,
              to: lk.to,
              kind: lk.kind || "",
              label: lk.label || "",
              color: lk.color || "#94a3b8",
            });
          }
        }
        return out;
      });

      // 顶层分支数 = 虚拟根派出去的路数
      const topCount = computed(
        () => flat.value.nodes.filter((n) => !n.parentId).length
      );

      // 按泳道分列：同一个 lane 的节点在同一列
      const lanes = computed(() => {
        const map = new Map();
        for (const n of flat.value.nodes) {
          if (!map.has(n.lane)) map.set(n.lane, []);
          map.get(n.lane).push(n);
        }
        return Array.from(map.entries())
          .sort((a, b) => a[0] - b[0])
          .map(([lane, nodes]) => ({
            lane,
            color: nodes[0].color,
            nodes: nodes.sort((x, y) => x.depth - y.depth),
          }));
      });

      /**
       * 读语义锚点。读不到就返回 null——**这是「锚点缺失不乱飞」的落点**。
       *
       * 锚点用 data-anchor-key 标记，与 Lancer 的做法一致：
       * 曲线端点绑定在**语义标识**上，不是绑定在「第几个元素」上。
       * 这样列表顺序变了、节点插进来了，线依然连对。
       */
      function readAnchor(key) {
        const root = rootEl.value;
        if (!root) return null;
        const el = root.querySelector(`[data-anchor-key="${key}"]`);
        if (!el) return null;
        // 不可见（display:none / 隐藏的 tab）→ 读不到位置
        const rect = el.getBoundingClientRect();
        if (!rect.width && !rect.height) return null;
        const style = global.getComputedStyle ? global.getComputedStyle(el) : null;
        if (style && (style.display === "none" || style.visibility === "hidden")) {
          return null;
        }
        const rootRect = root.getBoundingClientRect();
        return {
          x: rect.left + rect.width / 2 - rootRect.left,
          y: rect.top + rect.height / 2 - rootRect.top,
        };
      }

      /**
       * 重算所有连线。
       *
       * 关键：**任一端锚点读不到 → 整条边跳过**，不画。
       * 宁可少一条线，也不把线画到旁边的节点上。
       */
      function recompute() {
        const root = rootEl.value;
        if (!root) {
          edges.value = [];
          semEdges.value = [];
          return;
        }
        // 整个组件不可见（切到别的 tab）→ 别算了，省 CPU
        if (!root.isConnected) return;
        const rr = root.getBoundingClientRect();
        if (!rr.width && !rr.height) {
          if (edges.value.length) edges.value = [];
          if (semEdges.value.length) semEdges.value = [];
          return;
        }
        const out = [];
        for (const link of flat.value.links) {
          const a = readAnchor("branch:" + link.from);
          const b = readAnchor("branch:" + link.to);
          if (!a || !b) continue;   // ← 锚点缺失：放弃这条线
          out.push({
            id: link.from + "->" + link.to,
            d: cubicPath(a, b),
            color: link.color,
            a, b,
          });
        }
        // 语义边：同一套锚点纪律——
        // 任一端读不到锚点就整条丢弃，绝不画到邻近节点上。
        //
        // 这条纪律对语义边比对树线更重要：
        // 树线画错，用户会看出「这个点连错人了」；
        // 语义线画错，用户会以为「这两个分支真的有关系」——
        // 假关系比没线更糟。
        const sout = [];
        for (const link of semanticEdges.value) {
          const a = readAnchor("branch:" + link.from);
          const b = readAnchor("branch:" + link.to);
          if (!a || !b) continue;   // ← 锚点缺失：放弃这条线
          sout.push({
            id: link.id,
            d: cubicPath(a, b),
            color: link.color,
            label: link.label,
            kind: link.kind,
            a, b,
          });
        }
        // 脏检查：坐标没变就别动 Vue 状态。
        // 不然每帧都 set 一次 → 触发重渲染 → 又改变布局 → 又触发重算，
        // 变成「渲染-重算」死循环。
        //
        // 比较用**数值容差**而不是字符串相等：getBoundingClientRect
        // 会返回亚像素浮点，字面相等在某些缩放比下会来回抖动，
        // 抖动就会每帧 set → 又是死循环。
        const old = edges.value;
        const EPS = 0.5;   // 半像素内视为没动
        if (old.length === out.length &&
            old.every((e, i) => {
              const n = out[i];
              return e.id === n.id &&
                     Math.abs(e.a.x - n.a.x) < EPS &&
                     Math.abs(e.a.y - n.a.y) < EPS &&
                     Math.abs(e.b.x - n.b.x) < EPS &&
                     Math.abs(e.b.y - n.b.y) < EPS;
            })) {
          // 树线没动，但语义边可能动了（数据变了）→ 单独判一次
          const sold = semEdges.value;
          const sameSem = sold.length === sout.length &&
            sold.every((e, i) => {
              const n = sout[i];
              return e.id === n.id &&
                     Math.abs(e.a.x - n.a.x) < EPS &&
                     Math.abs(e.a.y - n.a.y) < EPS &&
                     Math.abs(e.b.x - n.b.x) < EPS &&
                     Math.abs(e.b.y - n.b.y) < EPS;
            });
          if (sameSem) return;
        }
        edges.value = out.map((e) => ({
          id: e.id, d: e.d, color: e.color, a: e.a, b: e.b,
        }));
        semEdges.value = sout.map((e) => ({
          id: e.id, d: e.d, color: e.color, a: e.a, b: e.b,
          label: e.label, kind: e.kind,
          // 提示框锚点：两端中点
          mx: (e.a.x + e.b.x) / 2,
          my: (e.a.y + e.b.y) / 2,
        }));
      }

      function scheduleRecompute() {
        if (raf) global.cancelAnimationFrame(raf);
        raf = global.requestAnimationFrame(() => {
          raf = 0;
          recompute();
        });
      }

      /**
       * 持续重算（rAF 循环）。
       *
       * 为什么不只在 mount/resize/数据变化时算？
       * 因为**布局还会因为别的原因变**：节点文本换行、面板折叠、
       * 字体加载完成、用户手动隐藏某个节点……
       * 这些都不会触发我们注册的任何事件，但锚点坐标已经变了。
       * 不重算 → 曲线停在旧位置，甚至指向已经消失的节点。
       *
       * Lancer 就是每帧重算（它是实时飞行动画，本来就需要）。
       * 墨溯借了它的思路，但加了脏检查——只有坐标真变了才更新 Vue 状态，
       * 所以「每帧算」的开销只花在几何计算上，不产生重渲染。
       */
      function loopTick() {
        recompute();
        loop = global.requestAnimationFrame(loopTick);
      }

      function onResize() {
        scheduleRecompute();
      }

      onMounted(() => {
        global.addEventListener("resize", onResize);
        nextTick(scheduleRecompute);
        // 持续重算：布局因任何原因变了，曲线都要跟上
        loop = global.requestAnimationFrame(loopTick);
      });

      onBeforeUnmount(() => {
        global.removeEventListener("resize", onResize);
        if (raf) global.cancelAnimationFrame(raf);
        if (loop) global.cancelAnimationFrame(loop);
      });

      // 分支数据变了（子代理跑完、状态更新）→ 重算连线位置
      watch(
        () => props.branches,
        () => nextTick(scheduleRecompute),
        { deep: true }
      );

      return {
        rootEl,
        lanes,
        edges,
        semEdges,
        semHover,
        topCount,
        nodeKey: (n) => n.id,
        // 语义边 hover：显示「这两个分支为什么有关系」
        semTip: (e) => (e ? e.label : ""),
        onSemEnter: (e) => { semHover.value = e; },
        onSemLeave: () => { semHover.value = null; },
        statusText: (n) => {
          if (n.status === "failed") return "失败";
          if (n.status === "running") return "进行中";
          if (n.status === "queued") return "排队中";
          // 「已送达」＝返回确实进了上层的上下文（程序判定）。
          // 措辞用「已回传」而不是「已核对」——
          // 「核对」是人的动作，程序只能说「送到了」。
          if (n.receipt === "accepted") return "已回传";
          return "待回传";
        },
        // 结算徽标（迭代 11）：父级显式表态采信了这条分支的结论。
        // 没有徽标 ≠ 出错——模型不写接受标记是常态，不是缺口。
        acceptBadge: (n) => (n.accepted ? "已采信" : ""),
        acceptTitle: (n) => {
          if (!n.accepted) return "";
          const by = n.acceptedBy === "main" ? "主代理" : "直接父";
          return by + "明确采信" + (n.acceptReason ? "：" + n.acceptReason : "");
        },
      };
    },
    template: `
    <div class="v2-bt" ref="rootEl">
      <!-- 连线层（树线）：SVG 铺在节点下面。
           树线是**编排关系**，视觉上属于"背景"，所以压在卡片下。 -->
      <svg class="v2-bt-lines" aria-hidden="true">
        <path v-for="e in edges" :key="e.id" class="v2-bt-line"
              :d="e.d" :style="{ stroke: e.color }"></path>
      </svg>

      <!-- 语义边图层（迭代 12）：独立的一层，**压在节点上面**。

           为什么必须单独一层、且必须在上面：
           - 语义边是**跨泳道**的：两个同层分支在同一行，
             连线必然横穿中间那些节点卡片。
           - 压在下面 → 线被卡片盖住，几乎看不见，
             而且鼠标命中的是卡片不是线 → hover 拿不到理由。
           - 压在上面 → 线横跨卡片，一眼看出「这是跨越关系，
             不是树的一部分」——虚线 + 弱化配色正好传达这一点。

           每条边画两遍：一条宽的透明「命中区」收 hover，
           一条细的可见虚线。命中区必须单独画——
           虚线本身是断续的，鼠标落在线段之间的空隙里就 hover 不到。 -->
      <svg class="v2-bt-sem-layer" aria-hidden="true">
        <path v-for="e in semEdges" :key="'h' + e.id" class="v2-bt-sem-hit"
              :d="e.d"
              @mouseenter="onSemEnter(e)" @mouseleave="onSemLeave()"></path>
        <path v-for="e in semEdges" :key="'s' + e.id" class="v2-bt-sem-line"
              :d="e.d" :style="{ stroke: e.color }"></path>
      </svg>

      <!-- 语义边提示：hover 时说明「这两个分支为什么有关系」 -->
      <div v-if="semHover" class="v2-bt-sem-tip"
           :style="{ left: semHover.mx + 'px', top: semHover.my + 'px' }">
        {{ semTip(semHover) }}
      </div>

      <!-- 虚拟根：代表主代理。只有一层分支时，靠它把「树」的拓扑画出来 -->
      <div class="v2-bt-root" data-anchor-key="branch:root" data-branch-id="root">
        <span class="v2-bt-root-dot"></span>
        <span class="v2-bt-root-label">主代理</span>
        <span class="v2-bt-root-sub">并行委派 {{ topCount }} 个子代理</span>
      </div>

      <!-- 泳道：一列一个顶层子代理 -->
      <div class="v2-bt-lanes">
        <div v-for="lane in lanes" :key="lane.lane" class="v2-bt-lane"
             :style="{ '--lc': lane.color }">
          <div class="v2-bt-lane-head">
            <span class="v2-bt-lane-dot"></span>
            <span class="v2-bt-lane-label">子代理 {{ lane.lane + 1 }}</span>
          </div>
          <div v-for="n in lane.nodes" :key="nodeKey(n)" class="v2-bt-node"
               :class="['st-' + n.status, 'd-' + n.depth]"
               :data-anchor-key="'branch:' + n.id"
               :data-branch-id="n.id">
            <div class="v2-bt-node-head">
              <span class="v2-bt-node-dot" :class="n.status"></span>
              <span class="v2-bt-node-goal">{{ n.goal }}</span>
            </div>
            <div class="v2-bt-node-meta">
              <span class="v2-bt-node-status">{{ statusText(n) }}</span>
              <span>{{ n.steps.length }} 次查询</span>
              <span>{{ n.cost }} 秒</span>
              <span v-if="n.depth > 1" class="v2-bt-node-sub">子代理的子代理</span>
              <span v-if="acceptBadge(n)" class="v2-bt-node-accept"
                    :title="acceptTitle(n)">{{ acceptBadge(n) }}</span>
            </div>
            <div v-if="n.error" class="v2-bt-node-err">{{ n.error }}</div>
            <div v-else-if="n.report" class="v2-bt-node-report">{{ n.report }}</div>
          </div>
        </div>
      </div>
    </div>`,
  };

  global.MosuV2 = global.MosuV2 || {};
  global.MosuV2.BranchTree = BranchTree;
})(window);
