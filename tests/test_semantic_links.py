# -*- coding: utf-8 -*-
"""跨分支语义关联的判据测试（迭代 12，对标 Lancer processTreeRoutes）。

这层要验的**不是**「函数返回了东西」，而是「该连的连、不该连的不连」。

为什么这么严
------------
语义边是**推断出来的关系**。判据一松，画出来的线就是假的，
而假关系比没线更糟——用户看到一条线，会以为「这两个分支真的有关系」。

所以判据必须按**真实证据样本**设计，而不是按「字段存在」设计
（迭代 10 的血泪：断言字段存在等于没验）。

三条核心纪律：
  ① 只有 **quote 类证据**才说明「真读到了原话」——
     stat 证据只是「搜到了这个会话」，把它算进去 → 满屏假线
  ② **父子之间不连**（树线已经表达了那个关系）
  ③ **同一对分支只给一条线**（两种判据同时成立也只出一条）

跑法：python -m pytest tests/test_semantic_links.py -p no:typeguard -q
"""
import unittest

from core.agent_team import (SEMANTIC_COLORS, TOPIC_MAX_OVERLAP,
                             TOPIC_MIN_OVERLAP, Branch, _all_branches,
                             attach_semantic_links, semantic_links)


def _branch(goal: str, quotes=(), parent: Branch | None = None,
            depth: int | None = None) -> Branch:
    """造一条分支。

    quotes 里每项是 (kind, sessionId, sender)：
      kind == "quote" → 真读到了原话
      kind == "stat" → 只查到了这个会话
    """
    b = Branch(goal, depth=depth if depth is not None
               else (1 if parent is None else 2), parent=parent)
    b.evidence = [{"kind": k, "sessionId": sid, "sender": snd, "text": "x"}
                  for (k, sid, snd) in quotes]
    b.status = "complete"
    return b


class TestSemanticLinkJudgement(unittest.TestCase):
    """判据本身。"""

    def test_quote_same_session_links(self):
        """两个分支都读了同一个会话的同一个人 → 连，判据 shared-session。"""
        a = _branch("查一下李小明最近说了什么",
                    [("quote", "wxid_zhang", "李小明")])
        b = _branch("看看李小明这个月心情怎么样",
                    [("quote", "wxid_zhang", "李小明")])
        links = semantic_links([a, b])
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["kind"], "shared-session")
        self.assertEqual(links[0]["from"], a.id)
        self.assertEqual(links[0]["to"], b.id)

    def test_stat_evidence_does_not_link(self):
        """只有 stat 证据 → **不连**。

        这是本组测试最重要的一条：`find_contact` 会为搜到的每个会话产一条
        stat 证据，把 stat 算进去 → 「查了张」的分支和任何分支都连上
        → 满屏假线。stat 只说明「搜到了」，不说明「读到了」。
        """
        a = _branch("查一下李小明", [("stat", "wxid_zhang", "李小明")])
        b = _branch("看看李小明", [("stat", "wxid_zhang", "李小明")])
        self.assertEqual(semantic_links([a, b]), [])

    def test_stat_and_quote_mixed_only_quote_counts(self):
        """一个分支全是 stat、另一个有 quote → 不连。"""
        a = _branch("查一下李小明", [("stat", "wxid_zhang", "李小明")])
        b = _branch("读李小明的聊天",
                    [("stat", "wxid_zhang", "李小明"),
                     ("quote", "wxid_zhang", "李小明")])
        self.assertEqual(semantic_links([a, b]), [])

    def test_different_sessions_same_sender_still_links(self):
        """同一个会话 → 连（这是判据 1 的定义）。"""
        a = _branch("A", [("quote", "s1", "李小明")])
        b = _branch("B", [("quote", "s1", "李小明")])
        self.assertEqual(len(semantic_links([a, b])), 1)

    def test_parent_child_not_linked(self):
        """父子之间不画语义线——树线已经表达了那个关系。"""
        parent = _branch("父任务", [("quote", "wxid_zhang", "李小明")])
        child = _branch("子任务", [("quote", "wxid_zhang", "李小明")],
                        parent=parent)
        other = _branch("另一支", [("quote", "wxid_zhang", "李小明")])
        links = semantic_links([parent, other])
        # parent-other 是同辈 → 连
        self.assertTrue(any({l["from"], l["to"]} == {parent.id, other.id}
                            for l in links))
        # parent-child 是父子 → 不连
        self.assertFalse(any({l["from"], l["to"]} == {parent.id, child.id}
                             for l in links))

    def test_no_self_loop(self):
        """不能出现「自己连自己」。

        构造 Branch(parent=P) 会自动 P.children.append(self)，
        摊平整棵树时同一个分支可能出现在两处 → 自环。
        """
        parent = _branch("父", [("quote", "s1", "张三")])
        child = _branch("子", [("quote", "s1", "张三")], parent=parent)
        links = semantic_links([parent, child])
        self.assertEqual([l for l in links if l["from"] == l["to"]], [])

    def test_duplicate_branch_id_deduped(self):
        """同一分支同时出现在顶层列表和 parent.children 里 → 只算一次。"""
        parent = _branch("父", [("quote", "s1", "张三")])
        child = _branch("子", [("quote", "s1", "张三")], parent=parent)
        flat = _all_branches([parent, child])
        self.assertEqual(len(flat), len({b.id for b in flat}))

    def test_child_not_appended_twice(self):
        """孙分支在 parent.children 里**只能出现一次**。

        真 bug（迭代 12 端到端抓到）：Branch.__init__ 里已经
        `parent.children.append(self)`，而 agent_team 拿到二次委派结果后
        又 `br.children.extend(kids)` → 同一个孙分支在树里出现两次
        → 前端把孙节点画了两遍。
        """
        parent = _branch("父", [("quote", "s1", "张三")])
        child = _branch("子", [("quote", "s1", "张三")], parent=parent)
        # 模拟 agent_team 的二次委派路径（曾经的 bug 写法）
        known = {c.id for c in parent.children}
        parent.children.extend([c for c in [child] if c.id not in known])
        ids = [c.id for c in parent.children]
        self.assertEqual(len(ids), len(set(ids)),
                         f"children 里有重复分支：{ids}")

    def test_one_pair_one_link_only(self):
        """同一对分支即使两种判据都成立，也只给一条线。

        同时给「同一个会话」+「同一个话题」两条，
        用户会以为有两件事，其实是一件事的两种说法——噪音。
        """
        a = _branch("查一下李小明最近说了什么",
                    [("quote", "wxid_zhang", "李小明")])
        b = _branch("看看李小明这个月心情怎么样",
                    [("quote", "wxid_zhang", "李小明")])
        links = semantic_links([a, b])
        self.assertEqual(len(links), 1)

    def test_label_uses_readable_name_not_wxid(self):
        """标签必须是人名，不能是 wxid。

        sessionId 是 wxid_xxx / 群 username，用户看不懂
        「都查过 wxid_xxx」。所以标签取 quote 证据里的 sender。
        """
        a = _branch("A", [("quote", "wxid_zhang", "李小明")])
        b = _branch("B", [("quote", "wxid_zhang", "李小明")])
        links = semantic_links([a, b])
        self.assertIn("李小明", links[0]["label"])
        self.assertNotIn("wxid_zhang", links[0]["label"])

    def test_no_link_when_nothing_shared(self):
        """毫无交集 → 不连。"""
        a = _branch("查李小明", [("quote", "s1", "李小明")])
        b = _branch("查李四", [("quote", "s2", "李四")])
        self.assertEqual(semantic_links([a, b]), [])

    def test_single_branch_no_links(self):
        a = _branch("只有一个", [("quote", "s1", "张三")])
        self.assertEqual(semantic_links([a]), [])
        self.assertEqual(semantic_links([]), [])

    def test_link_has_required_fields(self):
        """每条线必须有 kind / label / color / from / to，缺一前端画不出来。"""
        a = _branch("A", [("quote", "s1", "张三")])
        b = _branch("B", [("quote", "s1", "张三")])
        links = semantic_links([a, b])
        for lk in links:
            for k in ("from", "to", "kind", "label", "color"):
                self.assertIn(k, lk)
            self.assertIn(lk["color"], SEMANTIC_COLORS.values())

    def test_no_time_overlap_criterion(self):
        """时间重叠**不能**当判据。

        并行委派下三个子代理两两时间都重叠 → 每对都连 → 纯噪音。
        这条判据被砍掉过，这里锁死防止有人加回来。
        """
        src = open("core/agent_team.py", encoding="utf-8").read()
        self.assertNotIn("time-overlap", src)
        self.assertNotIn("TIME_OVERLAP_MIN", src)


class TestAttachSemanticLinks(unittest.TestCase):
    """挂载到分支 dict 上（前端按分支 id 取）。"""

    def test_both_ends_get_the_link(self):
        """两端都挂一份——前端画线时不用反查。"""
        a = _branch("A", [("quote", "s1", "张三")])
        b = _branch("B", [("quote", "s1", "张三")])
        links = semantic_links([a, b])
        dicts = [a.to_dict(), b.to_dict()]
        attach_semantic_links(dicts, links)
        self.assertEqual(len(dicts[0]["semanticLinks"]), 1)
        self.assertEqual(len(dicts[1]["semanticLinks"]), 1)
        self.assertEqual(dicts[0]["semanticLinks"][0]["id"]
                         if "id" in dicts[0]["semanticLinks"][0]
                         else dicts[0]["semanticLinks"][0]["kind"],
                         "shared-session")

    def test_recurses_into_children(self):
        """孙分支也要挂上——关联可能发生在隔代之间。"""
        root = _branch("根", [("quote", "s1", "张三")])
        kid = _branch("子", [("quote", "s1", "张三")], parent=root)
        other = _branch("另一支", [("quote", "s1", "张三")])
        links = semantic_links([root, other])
        dicts = [root.to_dict(), other.to_dict()]
        attach_semantic_links(dicts, links)
        # root 的 dict 里 children 是嵌套的，kid 也要挂上
        kid_dict = dicts[0]["children"][0]
        self.assertEqual(len(kid_dict["semanticLinks"]), 1)

    def test_branch_without_links_gets_empty_list(self):
        """没有关联的分支 → 空列表（前端不用判 undefined）。"""
        a = _branch("A", [("quote", "s1", "张三")])
        b = _branch("B", [("quote", "s2", "李四")])
        links = semantic_links([a, b])
        dicts = [a.to_dict(), b.to_dict()]
        attach_semantic_links(dicts, links)
        self.assertEqual(dicts[0]["semanticLinks"], [])
        self.assertEqual(dicts[1]["semanticLinks"], [])

    def test_empty_input_is_safe(self):
        attach_semantic_links([], [])
        attach_semantic_links(None, None)


class TestSemanticLinkWiring(unittest.TestCase):
    """**反查测试**：语义关联必须有真实的生产调用点。

    迭代 11 的血泪：「实现了语义但没接线」——
    函数写好了、测试也过了，但没有任何地方调用它，
    用户永远看不到这个功能。所以这里扫源码找调用点。
    """

    def test_engine_computes_links(self):
        """agent_engine 必须在跑完后算语义关联。"""
        src = open("core/agent_engine.py", encoding="utf-8").read()
        self.assertIn("semantic_links", src)
        self.assertIn("_semantic_links_of", src)

    def test_server_sends_links_to_frontend(self):
        """GUI 服务必须把语义关联下发给前端。"""
        src = open("gui/server.py", encoding="utf-8").read()
        self.assertIn("semanticLinks", src)
        self.assertIn("attach_semantic_links", src)

    def test_links_are_persisted(self):
        """必须落盘——不落盘，历史回看就只剩一棵树，关联信息全丢。"""
        src = open("gui/server.py", encoding="utf-8").read()
        self.assertIn('"semanticLinks": out.get("semanticLinks", [])', src)

    def test_frontend_renders_semantic_layer(self):
        """前端必须有独立的语义边图层（虚线），跟树线分开。"""
        src = open("gui/assets/v2/views-branch.js", encoding="utf-8").read()
        self.assertIn("semanticEdges", src)
        self.assertIn("v2-bt-sem-line", src)
        self.assertIn("v2-bt-sem-hit", src)


if __name__ == "__main__":
    unittest.main()
