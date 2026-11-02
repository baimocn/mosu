# -*- coding: utf-8 -*-
"""子树结算 + 回执协议（迭代 11，对标 Lancer 的 settled() / receipt）。

为什么单独一个文件
------------------
迭代 11 之前，墨溯的 `settle_children()` **定义完就没人调用**，
`RECEIPT_ACCEPTED` 常量也**一次都没被赋值过**——
也就是说「父级核对」和「子树结算」这两件事在代码里根本不存在。

这不是"少写了几行"，是**能力矩阵上写着「有」的东西其实没有**。
所以这一份单测的作用是：把「跑完 ≠ 结算」这个语义钉死，
别再出现"定义了个函数就算实现了"的情况。

Lancer 的原话（抄录）：
    "A subtree settles only after its immediate parent consumed the descendant returns."
    —— 子树只有在直接父消费了后代返回之后才结算。
"""
import unittest


class TestReceiptProtocol(unittest.TestCase):
    """回执：reported → accepted。父级显式接受。"""

    def test_new_branch_starts_as_reported(self):
        from core.agent_team import Branch, RECEIPT_REPORTED
        b = Branch("查一下张三", 0)
        self.assertEqual(b.receipt, RECEIPT_REPORTED)

    def test_consume_marks_accepted(self):
        from core.agent_team import Branch, RECEIPT_ACCEPTED, consume_branch
        b = Branch("查一下张三", 0)
        consume_branch(b, "main")
        self.assertEqual(b.receipt, RECEIPT_ACCEPTED)
        self.assertEqual(b.consumed_by, "main")
        self.assertGreater(b.consumed_at, 0)

    def test_consume_is_idempotent(self):
        """重复消费不应改写首次消费方——否则「谁消费了」会被抢走。"""
        from core.agent_team import Branch, consume_branch
        b = Branch("查一下张三", 0)
        consume_branch(b, "main")
        first_at = b.consumed_at
        consume_branch(b, "other")
        self.assertEqual(b.consumed_by, "main")
        self.assertEqual(b.consumed_at, first_at)

    def test_consumed_by_is_direct_parent_not_main(self):
        """孙分支的消费方必须是它的**直接父**，不能是主代理。

        混成 "main" 就等于跳过了「直接父消费」这一环，
        Lancer settled() 的语义就废了。
        """
        from core.agent_team import Branch, consume_branch
        parent = Branch("父", 0)
        kid = Branch("孙", 1, parent=parent)
        consume_branch(kid, parent.id)
        self.assertEqual(kid.consumed_by, parent.id)
        self.assertNotEqual(kid.consumed_by, "main")


class TestSubtreeSettlement(unittest.TestCase):
    """结算：跑完 ≠ 结算。返回必须被直接父消费过。"""

    def test_leaf_branch_settled_when_complete(self):
        from core.agent_team import Branch, BRANCH_COMPLETE, branch_settled
        b = Branch("查一下张三", 0)
        b.status = BRANCH_COMPLETE
        self.assertTrue(branch_settled(b))

    def test_running_branch_not_settled(self):
        from core.agent_team import Branch, BRANCH_RUNNING, branch_settled
        b = Branch("查一下张三", 0)
        b.status = BRANCH_RUNNING
        self.assertFalse(branch_settled(b))

    def test_failed_branch_counts_as_settled(self):
        """失败的分支也算终态——它不会再来回执了，不算就永远悬着。"""
        from core.agent_team import Branch, BRANCH_FAILED, branch_settled
        b = Branch("查一下张三", 0)
        b.status = BRANCH_FAILED
        self.assertTrue(branch_settled(b))

    def test_parent_not_settled_while_child_unconsumed(self):
        """核心语义：父分支跑完了，但子分支的返回没被消费 → 不算结算。"""
        from core.agent_team import Branch, BRANCH_COMPLETE, branch_settled
        parent = Branch("父", 0)
        parent.status = BRANCH_COMPLETE
        kid = Branch("子", 1, parent=parent)
        kid.status = BRANCH_COMPLETE
        self.assertFalse(branch_settled(parent))

    def test_parent_settled_after_child_consumed(self):
        from core.agent_team import (Branch, BRANCH_COMPLETE, branch_settled,
                                     consume_branch)
        parent = Branch("父", 0)
        parent.status = BRANCH_COMPLETE
        kid = Branch("子", 1, parent=parent)
        kid.status = BRANCH_COMPLETE
        consume_branch(kid, parent.id)
        self.assertTrue(branch_settled(parent))

    def test_settle_children_requires_consumption(self):
        from core.agent_team import (Branch, BRANCH_COMPLETE, settle_children,
                                     consume_branch)
        parent = Branch("父", 0)
        parent.status = BRANCH_COMPLETE
        kid = Branch("子", 1, parent=parent)
        kid.status = BRANCH_COMPLETE
        self.assertFalse(settle_children([parent]))
        consume_branch(kid, parent.id)
        self.assertTrue(settle_children([parent]))

    def test_consume_tree_marks_children_by_direct_parent(self):
        """consume_tree 把子分支标记为被**它自己**消费（不是 main）。"""
        from core.agent_team import Branch, consume_tree
        parent = Branch("父", 0)
        kid = Branch("子", 1, parent=parent)
        consume_tree([parent], "main")
        self.assertEqual(parent.consumed_by, "main")
        self.assertEqual(kid.consumed_by, parent.id)


class TestExplicitAcceptance(unittest.TestCase):
    """显式接受：父级在文本里写 `mosu-accepted: <分支id>` 表态采信。

    对标 Lancer 的 `explicitAcceptanceEvidenceFromParentMessage`
    （app-store-orchestration.ts:1929）。

    **关键**：这一层是**语义层**，**不参与结算判定**。
    Lancer 的结算门槛是「投递确认」，不是「模型说了采纳」——
    Lancer 全仓没有任何提示词教模型写 orchestrator-accepted，
    照抄成门槛等于结算永远为假。
    """

    def test_parse_marks(self):
        from core.agent_team import parse_accept_marks
        text = ("这是结论。\n"
                "mosu-accepted: b1234abcd 因为他主动找过你三次\n"
                "mosu-accepted: b5678efgh")
        marks = parse_accept_marks(text)
        self.assertEqual(len(marks), 2)
        self.assertEqual(marks[0][0], "b1234abcd")
        self.assertIn("主动找过你三次", marks[0][1])
        self.assertEqual(marks[1][0], "b5678efgh")

    def test_parse_ignores_normal_text(self):
        from core.agent_team import parse_accept_marks
        self.assertEqual(parse_accept_marks("普通答案，没有任何标记"), [])
        # 大小写不敏感（Lancer 的正则带 /i）
        self.assertEqual(len(parse_accept_marks("MOSU-ACCEPTED: b1234")), 1)

    def test_apply_marks_sets_branch(self):
        from core.agent_team import Branch, apply_accept_marks
        b = Branch("查一下张三", 0)
        apply_accept_marks("mosu-accepted: " + b.id + " 理由", [b], "main")
        self.assertTrue(b.accepted)
        self.assertEqual(b.accepted_by, "main")
        self.assertEqual(b.accept_reason, "理由")

    def test_apply_marks_ignores_unknown_id(self):
        """模型可能编一个不存在的分支 id —— 必须忽略，不能新建分支。"""
        from core.agent_team import Branch, apply_accept_marks
        b = Branch("查一下张三", 0)
        hit = apply_accept_marks("mosu-accepted: b99999999", [b], "main")
        self.assertEqual(hit, [])
        self.assertFalse(b.accepted)

    def test_apply_marks_is_idempotent(self):
        from core.agent_team import Branch, apply_accept_marks
        b = Branch("查一下张三", 0)
        apply_accept_marks(f"mosu-accepted: {b.id} 第一次", [b], "main")
        first_at = b.accepted_at
        apply_accept_marks(f"mosu-accepted: {b.id} 第二次", [b], "other")
        self.assertEqual(b.accepted_by, "main")
        self.assertEqual(b.accepted_at, first_at)

    def test_acceptance_does_not_affect_settlement(self):
        """核心不变量：**显式接受不改变结算判定**。

        理由：结算门槛是「送达」（程序判定）。
        如果接受标记能左右结算，模型不写标记就永远「未结算」——
        那等于把能力押在模型自觉上，Lancer 自己都没这么干。
        """
        from core.agent_team import (Branch, BRANCH_COMPLETE, branch_settled,
                                     apply_accept_marks)
        parent = Branch("父", 0)
        parent.status = BRANCH_COMPLETE
        kid = Branch("子", 1, parent=parent)
        kid.status = BRANCH_COMPLETE
        # 只送达，不表态 → 不结算
        self.assertFalse(branch_settled(parent))
        # 表态了，但没送达 → **还是不结算**
        apply_accept_marks(f"mosu-accepted: {kid.id}", [kid], parent.id)
        self.assertTrue(kid.accepted)
        self.assertFalse(branch_settled(parent))

    def test_engine_applies_marks_from_answer(self):
        """引擎层：主代理答案里的标记要真的落到分支上。"""
        from core.agent_engine import AgentRun, _apply_accept_marks
        from core.agent_team import Branch
        run = AgentRun("测试")
        b = Branch("查一下张三", 0)
        run.branches.append(b)
        run.answer = f"结论如上。\nmosu-accepted: {b.id} 数据对得上"
        _apply_accept_marks(run)
        self.assertTrue(b.accepted)
        self.assertEqual(b.accepted_by, "main")


class TestDeliveryTiming(unittest.TestCase):
    """送达点：必须是「payload 回喂之后」，不是「分支跑完之后」。

    Lancer 的顺序（app-store.ts:3571-3586）：
      ① submitComposerToSession(把报告投进父会话) 成功
      ② 确认父会话 transcript 里确实出现了该报告
      ③ 才把回执从 reported 翻成 accepted

    早一步标 accepted = 自己给自己签字。
    """

    def test_receipt_starts_reported(self):
        from core.agent_team import Branch, RECEIPT_REPORTED
        b = Branch("查一下张三", 0)
        b.status = "complete"
        self.assertEqual(b.receipt, RECEIPT_REPORTED)
        self.assertEqual(b.consumed_by, "")

    def test_consume_then_settles(self):
        from core.agent_team import (Branch, BRANCH_COMPLETE, branch_settled,
                                     consume_branch)
        b = Branch("查一下张三", 0)
        b.status = BRANCH_COMPLETE
        consume_branch(b, "main")
        self.assertTrue(branch_settled(b))


class TestSettlementReport(unittest.TestCase):
    """结算状态要如实反映给用户，不能默默当「都查完了」。"""

    def _run(self):
        from core.agent_engine import AgentRun
        return AgentRun("测试问题")

    def test_no_branches_is_settled(self):
        from core.agent_engine import _settlement_of
        st = _settlement_of(self._run())
        self.assertTrue(st["settled"])
        self.assertEqual(st["total"], 0)

    def test_counts_all_descendants(self):
        """总数必须含孙分支——只数顶层就看不出结算缺口。"""
        from core.agent_engine import _settlement_of
        from core.agent_team import Branch, BRANCH_COMPLETE
        run = self._run()
        parent = Branch("父", 0)
        parent.status = BRANCH_COMPLETE
        kid = Branch("子", 1, parent=parent)
        kid.status = BRANCH_COMPLETE
        run.branches.append(parent)
        st = _settlement_of(run)
        self.assertEqual(st["total"], 2)

    def test_pending_listed_when_not_consumed(self):
        from core.agent_engine import _settlement_of
        from core.agent_team import Branch, BRANCH_COMPLETE
        run = self._run()
        parent = Branch("父", 0)
        parent.status = BRANCH_COMPLETE
        kid = Branch("子", 1, parent=parent)
        kid.status = BRANCH_COMPLETE
        run.branches.append(parent)
        st = _settlement_of(run)
        self.assertFalse(st["settled"])
        self.assertEqual(st["consumed"], 0)
        self.assertIn(kid.id, st["pending"])

    def test_all_consumed_means_settled(self):
        from core.agent_engine import _settlement_of
        from core.agent_team import Branch, BRANCH_COMPLETE, consume_tree
        run = self._run()
        parent = Branch("父", 0)
        parent.status = BRANCH_COMPLETE
        kid = Branch("子", 1, parent=parent)
        kid.status = BRANCH_COMPLETE
        run.branches.append(parent)
        consume_tree([parent], "main")
        st = _settlement_of(run)
        self.assertTrue(st["settled"])
        self.assertEqual(st["consumed"], 2)
        self.assertEqual(st["pending"], [])

    def test_note_appended_when_unsettled(self):
        """未结算必须在答案里说清楚——否则用户以为都查完了。"""
        from core.agent_engine import _settlement_note
        from core.agent_team import Branch, BRANCH_COMPLETE
        run = self._run()
        parent = Branch("父", 0)
        parent.status = BRANCH_COMPLETE
        kid = Branch("子", 1, parent=parent)
        kid.status = BRANCH_COMPLETE
        run.branches.append(parent)
        run.answer = "这是答案正文。"
        run.settlement = {"settled": False, "total": 2, "consumed": 0,
                          "pending": [kid.id]}
        out = _settlement_note(run)
        self.assertIn("分支未结算", out)
        self.assertIn(kid.id, out)
        self.assertIn("这是答案正文。", out)

    def test_no_note_when_settled(self):
        from core.agent_engine import _settlement_note
        run = self._run()
        run.answer = "这是答案正文。"
        run.settlement = {"settled": True, "total": 1, "consumed": 1,
                          "pending": []}
        self.assertEqual(_settlement_note(run), "这是答案正文。")


class TestSettlementIsWired(unittest.TestCase):
    """接线检查：这些函数不能又变成"定义了没人调"。"""

    def test_agent_run_has_settlement_field(self):
        from core.agent_engine import AgentRun
        self.assertEqual(AgentRun("q").settlement, {})

    def test_to_dict_exposes_settlement(self):
        from core.agent_engine import AgentRun
        run = AgentRun("q")
        run.settlement = {"settled": True, "total": 0, "consumed": 0,
                          "pending": []}
        self.assertIn("settlement", run.to_dict())

    def test_branch_to_dict_exposes_consumption(self):
        from core.agent_team import Branch, BRANCH_COMPLETE, consume_branch
        b = Branch("查一下张三", 0)
        # 必须先置终态：settled 要求「自身终态 + 子分支都被消费」。
        # 分支刚建出来是 queued，此时 settled 自然是 False（这是对的，
        # 不是 bug——跑都没跑完，谈不上结算）。
        b.status = BRANCH_COMPLETE
        consume_branch(b, "main")
        d = b.to_dict()
        self.assertEqual(d["consumedBy"], "main")
        self.assertTrue(d["settled"])
        self.assertEqual(d["receipt"], "accepted")

    def test_settle_children_actually_called_somewhere(self):
        """反查：settle_children 必须在生产代码里被引用过。

        这是本文件最重要的一条——迭代 11 的起因就是
        「函数定义了但没人调用」。写死一条测试守着。
        """
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        hits = []
        for p in root.glob("core/*.py"):
            txt = p.read_text(encoding="utf-8", errors="ignore")
            if "settle_children(" in txt and "def settle_children" not in txt:
                hits.append(p.name)
        self.assertTrue(hits, "settle_children 在 core/ 下没有任何调用点")

    def test_receipt_accepted_actually_assigned_somewhere(self):
        """反查：RECEIPT_ACCEPTED 必须有赋值点，不能只是常量定义。"""
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        hits = []
        for p in root.glob("core/*.py"):
            txt = p.read_text(encoding="utf-8", errors="ignore")
            if "receipt = RECEIPT_ACCEPTED" in txt:
                hits.append(p.name)
        self.assertTrue(hits, "RECEIPT_ACCEPTED 从没被赋值过")
