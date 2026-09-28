import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import CorpusDB, DomainError


class CorpusFlowTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = CorpusDB(self.path)
        self.a1 = self.db.add_user("甲", "annotator")
        self.a2 = self.db.add_user("乙", "annotator")
        self.arb = self.db.add_user("仲裁", "arbitrator")
        self.mgr = self.db.add_user("管理", "manager")
        self.g = self.db.add_guideline("v1", "独立标注")
        self.batch = self.db.create_batch("测试批次", self.g)
        self.item1 = self.db.add_item(self.batch, 1, "这个版本很快。")
        self.item2 = self.db.add_item(self.batch, 2, "没有明显变化。")

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def test_full_annotation_disagreement_adjudication_freeze_flow(self):
        for item in (self.item1, self.item2):
            self.db.assign(item, self.a1)
            self.db.assign(item, self.a2)
        self.db.submit_annotation(self.item1, self.a1, "正向")
        self.db.submit_annotation(self.item1, self.a2, "中性")
        self.db.submit_annotation(self.item2, self.a1, "中性")
        self.db.submit_annotation(self.item2, self.a2, "中性")
        self.assertEqual(1, len(self.db.disagreements(self.batch)))
        with self.assertRaisesRegex(DomainError, "分歧"):
            self.db.freeze_batch(self.batch, self.mgr)
        self.db.adjudicate(self.item1, "正向", "速度描述构成明确正向倾向", self.arb)
        result = self.db.freeze_batch(self.batch, self.mgr)
        self.assertIsNotNone(result["metrics"]["pairwise_agreement"])
        exported = self.db.export_gold(self.batch)
        self.assertEqual(2, len(exported["records"]))
        self.assertEqual("adjudication", exported["records"][0]["source"])

    def test_answer_isolation_and_role_validation(self):
        self.db.assign(self.item1, self.a1)
        self.db.assign(self.item1, self.a2)
        self.db.add_discussion(self.item1, self.a2, "我认为是正向", True)
        secret = self.db.get_item_for_user(self.item1, self.a1)
        self.assertTrue(secret["discussions"][0]["hidden"])
        self.db.submit_annotation(self.item1, self.a1, "负向")
        visible = self.db.get_item_for_user(self.item1, self.a1)
        self.assertFalse(visible["discussions"][0].get("hidden", False))
        with self.assertRaisesRegex(DomainError, "标注员"):
            self.db.assign(self.item2, self.arb)

    def test_guideline_switch_keeps_old_annotations_and_freezes_current_version(self):
        v2 = self.db.add_guideline("v2", "弱情绪需要在备注中说明")
        for item in (self.item1, self.item2):
            self.db.assign(item, self.a1)
            self.db.assign(item, self.a2)
        self.db.submit_annotation(self.item1, self.a1, "正向")
        self.db.submit_annotation(self.item1, self.a2, "中性")
        self.db.submit_annotation(self.item2, self.a1, "中性")
        self.db.submit_annotation(self.item2, self.a2, "中性")
        self.db.adjudicate(self.item1, "正向", "v1 下整体语义明确积极", self.arb)

        with self.assertRaisesRegex(DomainError, "管理员"):
            self.db.switch_batch_guideline(self.batch, v2, self.arb)
        event = self.db.switch_batch_guideline(self.batch, v2, self.mgr)
        self.assertEqual(self.g, event["from_guideline_id"])
        self.assertEqual(v2, event["to_guideline_id"])
        self.assertEqual("v1", event["from_version"])
        self.assertEqual("v2", event["to_version"])

        before_new = self.db.get_item_for_user(self.item1, self.a1)
        self.assertEqual(v2, before_new["guideline_id"])
        self.assertIsNone(before_new["own_annotation"])
        self.assertEqual(["v1"], [row["guideline_version"] for row in before_new["own_annotations"]])

        self.db.submit_annotation(self.item1, self.a1, "正向", "新版仍明确")
        self.db.submit_annotation(self.item1, self.a2, "负向", "新版按反讽处理")
        self.db.submit_annotation(self.item2, self.a1, "中性")
        self.db.submit_annotation(self.item2, self.a2, "中性")

        after_new = self.db.get_item_for_user(self.item1, self.a1)
        self.assertEqual("v2", after_new["own_annotation"]["guideline_version"])
        self.assertEqual(["v1", "v2"], [row["guideline_version"] for row in after_new["own_annotations"]])
        counts = dict(self.db.conn.execute(
            "SELECT guideline_id, COUNT(*) FROM annotations WHERE item_id IN (?,?) GROUP BY guideline_id",
            (self.item1, self.item2),
        ).fetchall())
        self.assertEqual(4, counts[self.g])
        self.assertEqual(4, counts[v2])

        self.assertEqual(1, len(self.db.disagreements(self.batch)))
        self.db.adjudicate(self.item1, "正向", "v2 下结合语境仍为正向", self.arb)
        status = self.db.freeze_status(self.batch)
        self.assertTrue(status["ready_to_freeze"])
        self.assertEqual([], status["missing_item_ids"])

        metrics = self.db.consistency(self.batch)
        self.assertEqual(v2, metrics["guideline_id"])
        self.assertEqual("v2", metrics["guideline_version"])
        self.db.freeze_batch(self.batch, self.mgr)
        exported = self.db.export_gold(self.batch)
        self.assertEqual(v2, exported["guideline_id"])
        self.assertEqual("v2", exported["guideline_version"])
        self.assertTrue(all(row["guideline_version"] == "v2" for row in exported["records"]))
        self.assertEqual("adjudication", exported["records"][0]["source"])
        self.assertEqual(1, len(self.db.guideline_switches(self.batch)))
        with self.assertRaisesRegex(DomainError, "冻结后不能换版"):
            self.db.switch_batch_guideline(self.batch, self.g, self.mgr)

    def test_freeze_status_lists_current_version_missing_items(self):
        v2 = self.db.add_guideline("v2", "新版规则")
        for item in (self.item1, self.item2):
            self.db.assign(item, self.a1)
            self.db.assign(item, self.a2)
            self.db.submit_annotation(item, self.a1, "中性")
            self.db.submit_annotation(item, self.a2, "中性")

        self.db.switch_batch_guideline(self.batch, v2, self.mgr)
        status = self.db.freeze_status(self.batch)
        self.assertFalse(status["can_freeze"])
        self.assertEqual([self.item1, self.item2], status["missing_item_ids"])
        self.assertEqual(
            [[self.a1, self.a2], [self.a1, self.a2]],
            [row["missing_annotator_ids"] for row in status["missing_annotations"]],
        )

        self.db.submit_annotation(self.item1, self.a1, "正向")
        status = self.db.freeze_status(self.batch)
        self.assertEqual([self.item1, self.item2], status["missing_item_ids"])
        item1_status = next(row for row in status["missing_annotations"] if row["item_id"] == self.item1)
        self.assertEqual(1, item1_status["current_annotations"])
        self.assertEqual([self.a2], item1_status["missing_annotator_ids"])
        with self.assertRaisesRegex(DomainError, "当前指南标注未收齐"):
            self.db.freeze_batch(self.batch, self.mgr)


if __name__ == "__main__":
    unittest.main()
