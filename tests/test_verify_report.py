# -*- coding: utf-8 -*-
"""验证报告校验器的测试。

为什么这个校验器要有测试
------------------------
校验器本身是「判断别人有没有说谎」的工具。
如果它自己放过坏报告，那整套纪律就是空的。

所以这里不光测「好报告能过」，更测**坏报告必须被拒**——
每一类坏法各一例，且错误信息要说清是哪一条不合格。

跑法：python -m pytest tests/test_verify_report.py -p no:typeguard -q
"""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.verify_report import (  # noqa: E402
    LEVELS, MIN_NOT_RUN_LEN, REPO_ROOT, validate, validate_file,
)


def good_report(**over):
    """一份合格报告的骨架。"""
    data = {
        "title": "示例：某个功能的验证",
        "level": "L5",
        "date": "2026-11-02",
        "claims": [
            {"text": "真机点提问能看到实时轨迹", "level": "L5"},
            {"text": "协议层收发正确", "level": "L3"},
        ],
        "not_run": [
            "Windows 缩放 150% 下的曲线端点精度没有实测过",
        ],
        "evidence": ["core/verify_report.py"],
    }
    data.update(over)
    return data


class TestGoodReports(unittest.TestCase):

    def test_good_report_passes(self):
        self.assertEqual(validate(good_report()), [])

    def test_empty_not_run_is_allowed(self):
        """not_run 写成空数组是**允许的**——字段在就行。

        「这一轮确实全跑了」和「这一轮漏了什么」同样重要。
        但字段缺失不行（那是沉默）。
        """
        self.assertEqual(validate(good_report(not_run=[])), [])

    def test_string_claims_are_accepted(self):
        """claims 也接受纯字符串（按 L2 算，最保守）。"""
        data = good_report(claims=["跑通了", "也跑通了"], level="L2")
        self.assertEqual(validate(data), [])

    def test_local_evidence_may_be_absent(self):
        """.cache/ 是本机证据，不入库 → 别人 clone 后不该校验失败。"""
        data = good_report(evidence=[".cache/audit/whatever.py"])
        self.assertEqual(validate(data), [])


class TestMissingFields(unittest.TestCase):

    def test_missing_not_run_is_rejected(self):
        """**核心规则**：缺 not_run 字段 = 报告不合格。

        这是整套纪律的地基：沉默 = 假装全验了。

        注意这里断言的是**报「缺必填字段」**，不是「报类型不对」——
        两者都拒，但只有前者能证明「必填检查」本身在起作用。
        踩过的坑：类型检查会把「缺字段」也拦下，于是必填检查坏掉时
        测试照样全绿（反向验证失效）。
        """
        data = good_report()
        del data["not_run"]
        problems = validate(data)
        self.assertTrue(any("not_run" in p and "缺必填字段" in p
                            for p in problems),
                        f"缺 not_run 没按「缺必填字段」报：{problems}")

    def test_not_run_wrong_type_is_a_different_error(self):
        """类型错（不是缺字段）要报出不同的原因，别混在一起。"""
        data = good_report(not_run="没跑")
        problems = validate(data)
        self.assertTrue(any("必须是数组" in p for p in problems), str(problems))
        self.assertFalse(any("缺必填字段" in p for p in problems),
                         "类型错不该报成缺字段")

    def test_missing_level_is_rejected(self):
        data = good_report()
        del data["level"]
        self.assertTrue(validate(data))

    def test_missing_claims_is_rejected(self):
        data = good_report()
        del data["claims"]
        self.assertTrue(validate(data))

    def test_missing_evidence_is_rejected(self):
        data = good_report()
        del data["evidence"]
        self.assertTrue(validate(data))

    def test_missing_title_is_rejected(self):
        data = good_report()
        del data["title"]
        self.assertTrue(validate(data))


class TestLevelHonesty(unittest.TestCase):

    def test_illegal_level_is_rejected(self):
        problems = validate(good_report(level="L7"))
        self.assertTrue(any("level" in p for p in problems), str(problems))

    def test_inflated_level_is_rejected(self):
        """**虚报证据等级**必须被拒。

        声明 L6 但所有 claims 都是 L2 → L6 是虚报。
        """
        data = good_report(
            level="L6",
            claims=[
                {"text": "单测通过", "level": "L2"},
                {"text": "另一个单测", "level": "L2"},
            ],
        )
        problems = validate(data)
        self.assertTrue(any("虚报" in p for p in problems),
                        f"虚报等级没被拒：{problems}")

    def test_level_matching_strongest_claim_passes(self):
        """level 恰好等于最强 claim → 通过（不是虚报）。"""
        data = good_report(
            level="L5",
            claims=[
                {"text": "真机验过", "level": "L5"},
                {"text": "单测", "level": "L2"},
            ],
        )
        self.assertEqual(validate(data), [])

    def test_claim_level_outside_table_is_rejected(self):
        data = good_report(claims=[{"text": "x", "level": "L9"}])
        self.assertTrue(any("claims[0]" in p for p in validate(data)))

    def test_plain_string_claim_counts_as_l2(self):
        """纯字符串 claim 按 L2 算（最保守）→ 声明 L5 就是虚报。"""
        data = good_report(level="L5", claims=["只有文字"])
        self.assertTrue(any("虚报" in p for p in validate(data)))


class TestNotRunQuality(unittest.TestCase):

    def test_vague_scope_is_rejected(self):
        """「其他场景未验证」这种范围无法界定的，必须拒。"""
        data = good_report(not_run=["其他场景未验证"])
        problems = validate(data)
        self.assertTrue(any("not_run" in p for p in problems), str(problems))

    def test_too_short_is_rejected(self):
        """太短说不清要补什么。"""
        data = good_report(not_run=["没测"])
        self.assertTrue(any("not_run" in p for p in validate(data)))

    def test_wording_containing_weiyan_is_allowed(self):
        """**「未验」本身不是问题**——只要写清「什么」没验就合格。

        第一版按关键词黑名单（含「未验」）判空话，误伤了这条：
        「派活任务的取消/中止——协议里没有 cancel 语义，未验」
        它完全可执行（去补 cancel 语义），却因为带「未验」被拒。
        """
        data = good_report(not_run=[
            "派活任务的取消/中止没有验过：协议里根本没有 cancel 语义",
        ])
        self.assertEqual(validate(data), [])

    def test_empty_item_is_rejected(self):
        data = good_report(not_run=["", "  "])
        self.assertTrue(any("not_run" in p for p in validate(data)))

    def test_not_run_must_be_list(self):
        data = good_report(not_run="没跑")
        self.assertTrue(any("not_run" in p for p in validate(data)))

    def test_multiline_not_run_is_rejected(self):
        """一条必须是单行。

        踩过的坑：一条 not_run 写成跨行两行，整个报告变成非法 JSON，
        校验器只能报「读不出来」，看不出是哪条写坏了。
        """
        data = good_report(not_run=["第一条\n     第二行缩进"])
        problems = validate(data)
        self.assertTrue(any("换行" in p for p in problems), str(problems))


class TestEvidence(unittest.TestCase):

    def test_nonexistent_evidence_is_rejected(self):
        """证据必须能打开——写个不存在的路径等于没有证据。"""
        data = good_report(evidence=["core/绝对不存在.py"])
        problems = validate(data)
        self.assertTrue(any("evidence" in p for p in problems), str(problems))

    def test_existing_evidence_passes(self):
        self.assertEqual(validate(good_report(evidence=["core/verify_report.py"])), [])

    def test_evidence_must_be_list(self):
        data = good_report(evidence="core/verify_report.py")
        self.assertTrue(validate(data))


class TestClaimsQuality(unittest.TestCase):

    def test_empty_claims_is_rejected(self):
        """一条都不证明的报告没有意义。"""
        data = good_report(claims=[])
        problems = validate(data)
        self.assertTrue(any("claims" in p for p in problems), str(problems))

    def test_blank_claim_text_is_rejected(self):
        data = good_report(claims=[{"text": "  ", "level": "L3"}])
        self.assertTrue(any("claims[0]" in p for p in validate(data)))


class TestRepoReports(unittest.TestCase):

    """仓库里已入库的报告必须全部合格。"""

    def test_all_committed_reports_pass(self):
        d = ROOT / "docs" / "verification"
        self.assertTrue(d.is_dir(), "docs/verification 目录不存在")
        files = sorted(d.glob("*.json"))
        self.assertTrue(files, "docs/verification 下没有报告")
        for f in files:
            problems = validate_file(f)
            self.assertEqual(problems, [], f"{f.name} 不合格：{problems}")

    def test_cli_exit_code_on_good_dir(self):
        r = subprocess.run(
            [sys.executable, "-m", "core.verify_report", "--dir",
             str(ROOT / "docs" / "verification")],
            capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("全部通过", r.stdout)

    def test_cli_exit_code_on_bad_report(self):
        """CLI 对坏报告必须**返回非 0 退出码**——否则 CI 里等于没跑。"""
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "bad.json"
            data = good_report()
            del data["not_run"]
            p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            r = subprocess.run(
                [sys.executable, "-m", "core.verify_report", str(p)],
                capture_output=True, text=True, cwd=str(ROOT))
            self.assertNotEqual(r.returncode, 0,
                                "坏报告竟然返回了 0 退出码")
            self.assertIn("not_run", r.stdout)

    def test_cli_reports_corrupt_json(self):
        """坏 JSON 也要被拒，不能崩栈。"""
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "broken.json"
            p.write_text("{ 这不是 json", encoding="utf-8")
            r = subprocess.run(
                [sys.executable, "-m", "core.verify_report", str(p)],
                capture_output=True, text=True, cwd=str(ROOT))
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn("Traceback", r.stderr)


class TestGradingTable(unittest.TestCase):

    def test_levels_are_l1_to_l6(self):
        """分级表是封闭的，且恰好 6 级。"""
        self.assertEqual(sorted(LEVELS), ["L1", "L2", "L3", "L4", "L5", "L6"])

    def test_levels_are_ordered(self):
        """强度必须单调递增——否则「级别」就没有意义。"""
        vals = [LEVELS[k] for k in sorted(LEVELS)]
        self.assertEqual(vals, sorted(vals))

    def test_repo_root_is_correct(self):
        """evidence 的相对基准是仓库根。"""
        self.assertEqual(REPO_ROOT, ROOT)
        self.assertTrue((REPO_ROOT / "core" / "verify_report.py").exists())

    def test_min_not_run_len_is_sane(self):
        self.assertGreaterEqual(MIN_NOT_RUN_LEN, 10)


class TestDisciplineIsWiredIn(unittest.TestCase):
    """纪律必须**接进项目**，不能只是一份躺在那儿的文档。"""

    def test_verification_doc_exists(self):
        self.assertTrue((ROOT / "docs" / "verification.md").exists(),
                        "docs/verification.md 不存在——分级规则没落地")

    def test_readme_points_to_verification_doc(self):
        """README 必须指向这份文档，否则没人找得到。"""
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("verification.md", readme)

    def test_lancer_e2e_scripts_are_committed(self):
        """对标验证脚本必须入库。

        原先全堆在 .cache/audit/（gitignored），换个人接手就没法复现验证。
        """
        d = ROOT / "tests" / "e2e" / "lancer"
        self.assertTrue(d.is_dir(), "tests/e2e/lancer 目录不存在")
        scripts = sorted(d.glob("e2e_iter*.py"))
        self.assertGreaterEqual(len(scripts), 4,
                                f"入库的迭代验证脚本只有 {len(scripts)} 个")
        for s in scripts:
            self.assertTrue((s.stat().st_size > 1000),
                            f"{s.name} 太小，可能是空壳")

    def test_e2e_scripts_locate_repo_root_dynamically(self):
        """脚本不能用硬编码的目录深度找仓库根（搬个位置就崩）。"""
        d = ROOT / "tests" / "e2e" / "lancer"
        for s in sorted(d.glob("*.py")):
            txt = s.read_text(encoding="utf-8")
            self.assertNotIn("parents[2]", txt,
                             f"{s.name} 还在用硬编码的 parents[2]")
            self.assertIn('(p / "core").is_dir()', txt,
                          f"{s.name} 没有动态定位仓库根")


if __name__ == "__main__":
    unittest.main()
