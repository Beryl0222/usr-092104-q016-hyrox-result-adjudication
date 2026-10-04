import unittest
from datetime import datetime, timedelta, timezone

from src.assembly import AssemblyBook
from src.model import (
    EVIDENCE_DUPLICATE,
    EVIDENCE_LATE,
    EVIDENCE_QUARANTINED,
    Evidence,
)

TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 20, 9, 0, tzinfo=TZ)


def make_evidence(
    evid,
    entry="e1",
    lane="L1",
    seg=0,
    source="timing_chip",
    minute=5,
    reported_delay=0,
    seq=None,
):
    occurred = T0 + timedelta(minutes=minute)
    return Evidence(
        evid,
        entry,
        lane,
        seg,
        source,
        occurred,
        occurred + timedelta(seconds=reported_delay),
        seq,
        {},
    )


class AssemblyTest(unittest.TestCase):
    def setUp(self):
        self.book = AssemblyBook(segment_count=16, grace_seconds=120)

    def test_duplicate_report_does_not_double_count(self):
        for j in range(3):
            self.book.ingest(make_evidence(f"pre-{j}", seg=j, minute=j + 1), "L1")
        first = make_evidence("a1", seg=3, minute=4, seq="dev-9")
        dup = make_evidence("a2", seg=3, minute=4, seq="dev-9")
        self.assertEqual(self.book.ingest(first, "L1"), [])
        self.assertEqual(self.book.ingest(dup, "L1"), [])
        split = self.book.split("e1", 3)
        self.assertEqual(split.evidence_ids, ["a1"])
        self.assertEqual(dup.status, EVIDENCE_DUPLICATE)
        self.assertTrue(any("重复上报" in n for n in split.notices))

    def test_late_report_merges_into_same_segment(self):
        for j in range(5):
            self.book.ingest(make_evidence(f"pre-{j}", seg=j, minute=j + 1), "L1")
        late = make_evidence("b1", seg=5, minute=6, reported_delay=600)
        self.assertEqual(self.book.ingest(late, "L1"), [])
        self.assertEqual(late.status, EVIDENCE_LATE)
        split = self.book.split("e1", 5)
        self.assertEqual(split.evidence_ids, ["b1"])
        self.assertTrue(any("迟到上报" in n for n in split.notices))
        # 迟到不新增环节：同一环节仍只有一条分段。
        self.assertEqual(len(self.book.splits_for("e1")), 6)

    def test_lane_mismatch_is_quarantined_for_review(self):
        stray = make_evidence("c1", lane="L2", seg=9, source="apparatus_sensor")
        drafts = self.book.ingest(stray, "L1")
        self.assertEqual(stray.status, EVIDENCE_QUARANTINED)
        self.assertEqual([d.kind for d in drafts], ["lane_mismatch"])
        self.assertEqual(drafts[0].evidence_ids, ("c1",))
        # 隔离记录不计入任何环节。
        self.assertIsNone(self.book.split("e1", 9))

    def test_unknown_segment_is_quarantined(self):
        out_of_range = make_evidence("d1", seg=16)
        drafts = self.book.ingest(out_of_range, "L1")
        self.assertEqual(out_of_range.status, EVIDENCE_QUARANTINED)
        self.assertEqual([d.kind for d in drafts], ["unknown_segment"])

    def test_sequence_gap_flagged_once(self):
        drafts = self.book.ingest(make_evidence("e1", seg=2, minute=15), "L1")
        gaps = [d for d in drafts if d.kind == "sequence_gap"]
        self.assertEqual([d.segment_index for d in gaps], [0, 1])
        # 更靠后的环节到达时，同一缺口不重复标记。
        drafts = self.book.ingest(make_evidence("e2", seg=3, minute=20), "L1")
        self.assertEqual([d for d in drafts if d.kind == "sequence_gap"], [])

    def test_time_inversion_flagged_but_counted(self):
        self.book.ingest(make_evidence("f1", seg=0, minute=5), "L1")
        inverted = make_evidence("f2", seg=1, minute=4)
        drafts = self.book.ingest(inverted, "L1")
        self.assertEqual([d.kind for d in drafts], ["time_inversion"])
        # 待审不处罚：记录仍计入分段。
        self.assertEqual(self.book.split("e1", 1).evidence_ids, ["f2"])

    def test_force_accept_after_ruling(self):
        stray = make_evidence("g1", lane="L2", seg=7)
        self.book.ingest(stray, "L1")
        self.book.force_accept(stray)
        split = self.book.split("e1", 7)
        self.assertEqual(split.evidence_ids, ["g1"])
        self.assertTrue(any("经认定采纳" in n for n in split.notices))


if __name__ == "__main__":
    unittest.main()
