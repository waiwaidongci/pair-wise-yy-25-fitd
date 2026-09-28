import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import CorpusDB, DomainError


class GuidelineSwitchTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = CorpusDB(self.path)
        self.a1 = self.db.add_user("甲", "annotator")
        self.a2 = self.db.add_user("乙", "annotator")
        self.arb = self.db.add_user("仲裁", "arbitrator")
        self.mgr = self.db.add_user("管理", "manager")
        self.g1 = self.db.add_guideline("v1", "旧规则")
        self.g2 = self.db.add_guideline("v2", "新规则")
        self.batch = self.db.create_batch("测试批次", self.g1)
        self.item1 = self.db.add_item(self.batch, 1, "这个版本很快。")
        self.item2 = self.db.add_item(self.batch, 2, "没有明显变化。")
        for item in (self.item1, self.item2):
            self.db.assign(item, self.a1)
            self.db.assign(item, self.a2)

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def _submit_v1(self):
        self.db.submit_annotation(self.item1, self.a1, "正向")
        self.db.submit_annotation(self.item1, self.a2, "中性")
        self.db.submit_annotation(self.item2, self.a1, "中性")
        self.db.submit_annotation(self.item2, self.a2, "中性")

    def test_switch_requires_manager_and_unfrozen(self):
        with self.assertRaisesRegex(DomainError, "管理员"):
            self.db.switch_guideline(self.batch, self.g2, self.a1)
        with self.assertRaisesRegex(DomainError, "指南"):
            self.db.switch_guideline(self.batch, 999, self.mgr)
        with self.assertRaisesRegex(DomainError, "已绑定"):
            self.db.switch_guideline(self.batch, self.g1, self.mgr)

    def test_old_annotations_kept_and_resent_under_new_version(self):
        self._submit_v1()
        switch = self.db.switch_guideline(self.batch, self.g2, self.mgr)
        self.assertEqual("v1", switch["from_version"])
        self.assertEqual("v2", switch["to_version"])

        # 旧标注按原指南保留：同一条目同一位标注员可以对 v2 再交一份
        self.db.submit_annotation(self.item1, self.a1, "负向", "按新规则重新判定")
        rows = self.db.conn.execute(
            "SELECT guideline_id,label FROM annotations WHERE item_id=? AND annotator_id=? ORDER BY guideline_id",
            (self.item1, self.a1),
        ).fetchall()
        self.assertEqual([(self.g1, "正向"), (self.g2, "负向")], [(r["guideline_id"], r["label"]) for r in rows])

        # 页面/接口能看到当前指南以及本人每个版本的标注
        view = self.db.get_item_for_user(self.item1, self.a1)
        self.assertEqual("v2", view["guideline_version"])
        versions = {r["guideline_version"]: r["label"] for r in view["own_annotations_by_guideline"]}
        self.assertEqual({"v1": "正向", "v2": "负向"}, versions)
        self.assertEqual("负向", view["own_annotation"]["label"])  # 默认展示当前版本

    def test_disagreement_consistency_freeze_only_see_current_version(self):
        self._submit_v1()
        # v1 有一条分歧并已仲裁
        self.assertEqual(1, len(self.db.disagreements(self.batch)))
        self.db.adjudicate(self.item1, "正向", "旧版规则下的明确倾向", self.arb)
        self.db.switch_guideline(self.batch, self.g2, self.mgr)

        # 切到 v2：旧分歧与旧仲裁不再影响当前视图
        self.assertEqual([], self.db.disagreements(self.batch))
        readiness = self.db.freeze_readiness(self.batch)
        self.assertEqual("v2", readiness["guideline_version"])
        self.assertFalse(readiness["ready"])
        missing_ordinals = {m["ordinal"] for m in readiness["missing_items"]}
        self.assertEqual({1, 2}, missing_ordinals)
        self.assertEqual(0, readiness["missing_items"][0]["annotation_count"])

        # v2 只收齐一条时，冻结入口继续提示缺另一条
        self.db.submit_annotation(self.item1, self.a1, "负向")
        self.db.submit_annotation(self.item1, self.a2, "负向")
        readiness = self.db.freeze_readiness(self.batch)
        self.assertEqual({2}, {m["ordinal"] for m in readiness["missing_items"]})
        with self.assertRaisesRegex(DomainError, r"item_id.*2"):
            self.db.freeze_batch(self.batch, self.mgr)

        # v2 全部收齐且无分歧后可冻结；一致性只统计 v2
        self.db.submit_annotation(self.item2, self.a1, "中性")
        self.db.submit_annotation(self.item2, self.a2, "中性")
        self.assertTrue(self.db.freeze_readiness(self.batch)["ready"])
        result = self.db.freeze_batch(self.batch, self.mgr)
        self.assertEqual("v2", result["guideline_version"])
        self.assertEqual(1.0, result["metrics"]["pairwise_agreement"])
        self.assertEqual("v2", self.db.export_gold(self.batch)["guideline_version"])

    def test_frozen_batch_cannot_switch_again(self):
        self._submit_v1()
        self.db.adjudicate(self.item1, "正向", "旧版规则下的明确倾向", self.arb)
        self.db.freeze_batch(self.batch, self.mgr)
        with self.assertRaisesRegex(DomainError, "冻结"):
            self.db.switch_guideline(self.batch, self.g2, self.mgr)

    def test_old_adjudication_kept_and_new_version_can_adjudicate(self):
        self._submit_v1()
        self.db.adjudicate(self.item1, "正向", "旧版规则下的明确倾向", self.arb)
        self.db.switch_guideline(self.batch, self.g2, self.mgr)
        # v2 制造分歧并仲裁
        self.db.submit_annotation(self.item1, self.a1, "负向")
        self.db.submit_annotation(self.item1, self.a2, "中性")
        self.db.submit_annotation(self.item2, self.a1, "中性")
        self.db.submit_annotation(self.item2, self.a2, "中性")
        self.assertEqual(1, len(self.db.disagreements(self.batch)))
        self.db.adjudicate(self.item1, "中性", "新版规则要求语义转折才算倾向", self.arb)
        rows = self.db.conn.execute(
            "SELECT guideline_id,final_label FROM adjudications WHERE item_id=? ORDER BY guideline_id",
            (self.item1,),
        ).fetchall()
        self.assertEqual([(self.g1, "正向"), (self.g2, "中性")], [(r["guideline_id"], r["final_label"]) for r in rows])

    def test_switch_and_freeze_results_queryable_in_snapshot(self):
        self._submit_v1()
        self.db.adjudicate(self.item1, "正向", "旧版规则下的明确倾向", self.arb)
        self.db.switch_guideline(self.batch, self.g2, self.mgr)
        snap = self.db.snapshot()
        switch = snap["guideline_switches"][-1]
        self.assertEqual(self.g2, switch["to_guideline_id"])
        self.assertEqual("v2", switch["to_version"])
        versions = {a["guideline_version"] for a in snap["annotations"] if a["item_id"] == self.item1}
        self.assertEqual({"v1"}, versions)  # 切换后尚未补标
        self.assertTrue(all(a["is_current"] is False for a in snap["annotations"]))

        self.db.submit_annotation(self.item1, self.a1, "负向")
        self.db.submit_annotation(self.item1, self.a2, "负向")
        self.db.submit_annotation(self.item2, self.a1, "中性")
        self.db.submit_annotation(self.item2, self.a2, "中性")
        self.db.freeze_batch(self.batch, self.mgr)
        snap = self.db.snapshot()
        self.assertEqual("frozen", next(b for b in snap["batches"] if b["id"] == self.batch)["status"])
        self.assertEqual(1, len(snap["freezes"]))
        self.assertIn("metrics", snap["freezes"][0])


if __name__ == "__main__":
    unittest.main()
