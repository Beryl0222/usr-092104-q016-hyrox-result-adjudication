import json
import unittest
from copy import deepcopy
from pathlib import Path

from src.engine import AdjudicationEngine
from src.model import verify_release_payload

EVENTS_PATH = Path(__file__).parents[1] / "data" / "sample_events.json"


def replay():
    engine = AdjudicationEngine()
    events = json.loads(EVENTS_PATH.read_text(encoding="utf-8"))
    for event in events:
        errors = engine.ingest(event)
        assert errors == [], f"{event['event_id']}: {errors}"
    return engine


def standings(release):
    return {r.entry_id: (r.standing, r.rank, r.total_seconds) for r in release.results}


class ScenarioReplayTest(unittest.TestCase):
    """完整联调场景：串道、换搭档、迟到/重复、医疗、申诉、改判、递补。"""

    @classmethod
    def setUpClass(cls):
        cls.engine = replay()

    def result(self, version, entry_id):
        release = self.engine.releases[version - 1]
        return next(r for r in release.results if r.entry_id == entry_id)

    def test_release_versions_and_statuses(self):
        self.assertEqual([r.version for r in self.engine.releases], [1, 2, 3, 4])
        self.assertEqual(
            [r.status for r in self.engine.releases],
            ["initial", "appeal_pending", "official", "official"],
        )

    def test_initial_release_holds_pending_and_medical(self):
        got = standings(self.engine.releases[0])
        # 校验异常只进入待审，不直接处罚；医疗停止不参与排名。
        self.assertEqual(got["e1"], ("pending_review", None, 4800.0))
        self.assertEqual(got["e2"], ("pending_review", None, None))
        self.assertEqual(got["e3"], ("medical_hold", None, None))

    def test_appeal_release_ranks_after_rulings(self):
        got = standings(self.engine.releases[1])
        self.assertEqual(got["e1"], ("ranked", 1, 4800.0))
        self.assertEqual(got["e2"], ("ranked", 2, 4840.0))
        self.assertEqual(got["e3"][0], "medical_hold")

    def test_official_release_includes_returned_athlete(self):
        got = standings(self.engine.releases[2])
        # e3 恢复参赛后完赛，并获志愿者误导减免 60 秒。
        self.assertEqual(got["e3"], ("ranked", 3, 8640.0))

    def test_correction_creates_new_official_version(self):
        got = standings(self.engine.releases[3])
        # 改判：e1 加罚 60 秒，名次在新版本中更替。
        self.assertEqual(got["e2"], ("ranked", 1, 4840.0))
        self.assertEqual(got["e1"], ("ranked", 2, 4860.0))
        self.assertEqual(got["e3"], ("ranked", 3, 8640.0))

    def test_adjacent_lane_evidence_reassigned(self):
        stray = self.engine.evidence["ev-stray-l4"]
        self.assertEqual(stray.status, "reassigned")
        derived = self.engine.evidence["ev-stray-l4#re:e2"]
        self.assertEqual(derived.status, "accepted")
        e2_seg9 = self.result(4, "e2").splits[9]
        self.assertIn("ev-stray-l4#re:e2", e2_seg9.evidence_ids)

    def test_anomalies_resolved_by_ruling_or_evidence(self):
        anomalies = self.engine.anomalies
        self.assertEqual(anomalies["ANX-1"].kind, "lane_mismatch")
        self.assertEqual(anomalies["ANX-1"].resolved_by, "r1")
        self.assertEqual(anomalies["ANX-2"].kind, "sequence_gap")
        self.assertEqual(anomalies["ANX-2"].resolved_by, "auto:evidence")
        self.assertEqual(anomalies["EXT-1"].resolved_by, "r2")
        self.assertTrue(all(a.status == "resolved" for a in anomalies.values()))

    def test_duplicate_and_late_reports_do_not_add_segments(self):
        e1 = self.result(4, "e1")
        self.assertEqual(len(e1.splits), 16)
        self.assertTrue(all(s.recorded_at for s in e1.splits))
        self.assertEqual(self.engine.evidence["e1-s3-dup"].status, "duplicate")
        self.assertTrue(any("重复上报" in n for n in e1.splits[3].notices))
        self.assertEqual(self.engine.evidence["e1-s5"].status, "late")
        self.assertTrue(any("迟到上报" in n for n in e1.splits[5].notices))

    def test_result_freezes_format_group_wave_partner_and_apparatus(self):
        freeze = self.result(4, "e1").freeze
        self.assertEqual(freeze.format_id, "fmt-hybrid-16")
        self.assertEqual(freeze.format_version, 1)
        self.assertEqual(freeze.group, "混双组")
        self.assertEqual(freeze.wave, "W1")
        # 赛前替换的搭档随成绩冻结。
        self.assertEqual(freeze.entry_version, 2)
        self.assertEqual(freeze.athlete_ids, ("张明", "王芳"))
        self.assertEqual(freeze.partner_eligibility["note"], "李华伤退，王芳替补")
        self.assertEqual(freeze.apparatus, {"AP-L3": "ok"})

    def test_penalty_reason_visible_to_participant(self):
        statement = self.engine.entry_statement("e1")
        self.assertEqual(statement["release"]["version"], 4)
        self.assertEqual(len(statement["splits"]), 16)
        self.assertEqual(len(statement["penalties"]), 1)
        penalty = statement["penalties"][0]
        self.assertEqual(penalty["ruling_id"], "r3")
        self.assertEqual(penalty["category"], "athlete_violation")
        self.assertEqual(penalty["seconds"], 60)
        self.assertIn("器械区违规", penalty["reason"])

    def test_medical_interval_visible(self):
        statement = self.engine.entry_statement("e3")
        self.assertEqual(len(statement["medical_intervals"]), 1)
        interval = statement["medical_intervals"][0]
        self.assertEqual(interval["authorized_by"], "医疗官-07")
        self.assertFalse(statement["under_medical_hold"])

    def test_conflict_report_locates_source(self):
        rows = [r for r in self.engine.conflict_report("e1") if r["kind"] == "lane_mismatch"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["resolved_by"], "r1")
        self.assertEqual(row["evidence"][0]["lane"], "L4")
        self.assertEqual(row["evidence"][0]["entry_lane"], "L3")
        self.assertEqual(row["evidence"][0]["source"], "apparatus_sensor")

    def test_certificates_follow_official_versions(self):
        certs_v3 = self.engine.issue_certificates("rel-2026", 3)
        self.assertEqual([c.entry_id for c in certs_v3], ["e1", "e2", "e3"])
        certs_v4 = self.engine.issue_certificates("rel-2026", 4)
        self.assertEqual([c.entry_id for c in certs_v4], ["e2", "e1", "e3"])
        old = self.engine.certificates["rel-2026-v3-e1"]
        self.assertEqual(old.status, "superseded")
        self.assertEqual(old.superseded_by, "rel-2026-v4-e1")
        # 同版本重复签发幂等。
        again = self.engine.issue_certificates("rel-2026", 4)
        self.assertEqual([c.certificate_id for c in again], [c.certificate_id for c in certs_v4])

    def test_award_backfill_after_correction(self):
        report = self.engine.award_report("rel-2026", 4)
        self.assertEqual(report["compared_to"], 3)
        self.assertEqual(report["awards"]["混双组"][0]["entry_id"], "e2")
        self.assertEqual(report["changes"]["混双组"], {"backfill": ["e2"], "revoked": ["e1"]})
        self.assertEqual(report["changes"]["个人组"], {"backfill": [], "revoked": []})

    def test_public_results_verifiable(self):
        self.assertEqual(self.engine.verify_chain("rel-2026"), [])
        exported = self.engine.export_release("rel-2026", 4)
        self.assertEqual(verify_release_payload(exported), [])
        tampered = deepcopy(exported)
        tampered["results"][0]["rank"] = 99
        self.assertTrue(verify_release_payload(tampered))
        public = self.engine.public_release("rel-2026", 4)
        self.assertEqual(public["ranked"][0]["entry_id"], "e2")
        self.assertEqual(public["digest"], exported["digest"])

    def test_auto_anomalies_logged_as_events(self):
        logged = [e for e in self.engine.event_log if e["event_type"] == "ANOMALY_FLAGGED"]
        kinds = [e["payload"]["kind"] for e in logged]
        self.assertIn("lane_mismatch", kinds)
        self.assertIn("sequence_gap", kinds)
        self.assertIn("external_report", kinds)


# ---- 小场景与错误路径 ----

def format_event():
    return {
        "event_id": "f1",
        "event_type": "FORMAT_PUBLISHED",
        "aggregate_type": "race_format",
        "aggregate_id": "fmt",
        "occurred_at": "2026-09-19T10:00:00+08:00",
        "version": 1,
        "summary": "赛制",
        "payload": {
            "segments": [
                {"index": 0, "kind": "run"},
                {"index": 1, "kind": "station", "station_id": "ST1"},
            ],
            "groups": ["个人组"],
            "waves": [{"wave_id": "W1", "starts_at": "2026-09-20T09:00:00+08:00"}],
            "rules": {},
        },
    }


def entry_event(entry_id="e1", version=1, lane="L1", athletes=("甲",), occurred="2026-09-19T12:00:00+08:00"):
    return {
        "event_id": f"en-{entry_id}-v{version}",
        "event_type": "ENTRY_CONFIRMED",
        "aggregate_type": "competition_entry",
        "aggregate_id": entry_id,
        "occurred_at": occurred,
        "version": version,
        "summary": "报名",
        "payload": {
            "format_id": "fmt",
            "group": "个人组",
            "wave": "W1",
            "lane": lane,
            "athlete_ids": list(athletes),
            "partner_eligibility": {"eligible": True},
        },
    }


def release_event(version, status, event_id=None):
    return {
        "event_id": event_id or f"rel-v{version}-{status}",
        "event_type": "RESULT_REPUBLISHED",
        "aggregate_type": "result_release",
        "aggregate_id": "rel",
        "occurred_at": f"2026-09-20T1{version}:00:00+08:00",
        "version": version,
        "summary": "发布",
        "payload": {"format_id": "fmt", "status": status},
    }


def base_engine():
    engine = AdjudicationEngine()
    assert engine.ingest(format_event()) == []
    assert engine.ingest(entry_event()) == []
    return engine


class EngineRuleTest(unittest.TestCase):
    def test_unauthorized_referee_rejected(self):
        engine = base_engine()
        assert engine.ingest({
            "event_id": "ax1",
            "event_type": "ANOMALY_FLAGGED",
            "aggregate_type": "competition_entry",
            "aggregate_id": "e1",
            "occurred_at": "2026-09-20T09:30:00+08:00",
            "version": 1,
            "summary": "外部异常",
            "payload": {"anomaly_id": "X1", "entry_id": "e1", "kind": "external_report", "detail": "待核"},
        }) == []
        errors = engine.ingest({
            "event_id": "ru1",
            "event_type": "RULING_SIGNED",
            "aggregate_type": "ruling",
            "aggregate_id": "r1",
            "occurred_at": "2026-09-20T09:40:00+08:00",
            "version": 1,
            "summary": "越权认定",
            "payload": {
                "category": "athlete_violation",
                "referee_id": "EQ-1",
                "referee_role": "equipment_referee",
                "entry_id": "e1",
                "anomaly_ids": ["X1"],
                "outcome": "penalty",
                "penalty_seconds": 30,
                "reason": "越权测试",
            },
        })
        self.assertTrue(any("无权认定" in e for e in errors))
        self.assertEqual(engine.anomalies["X1"].status, "pending")
        self.assertEqual(engine.rulings, {})

    def test_entry_version_must_increment(self):
        engine = base_engine()
        errors = engine.ingest(entry_event(version=3))
        self.assertTrue(any("递增" in e for e in errors))

    def test_entry_lane_is_immutable(self):
        engine = base_engine()
        errors = engine.ingest(entry_event(version=2, lane="L9"))
        self.assertTrue(any("不可变更" in e for e in errors))

    def test_late_partner_change_goes_to_review(self):
        engine = base_engine()
        errors = engine.ingest(entry_event(
            version=2, athletes=("甲", "乙"), occurred="2026-09-20T10:00:00+08:00"
        ))
        self.assertEqual(errors, [])
        kinds = [a.kind for a in engine.anomalies.values()]
        self.assertIn("late_partner_change", kinds)

    def test_release_version_and_status_rules(self):
        engine = base_engine()
        self.assertTrue(engine.ingest(release_event(2, "initial")))
        self.assertEqual(engine.ingest(release_event(1, "initial")), [])
        self.assertEqual(engine.ingest(release_event(2, "official")), [])
        errors = engine.ingest(release_event(3, "initial"))
        self.assertTrue(any("改判" in e for e in errors))

    def test_medical_flow_requires_authorization(self):
        engine = base_engine()
        no_case = engine.ingest({
            "event_id": "ret-1",
            "event_type": "RETURN_AUTHORIZED",
            "aggregate_type": "competition_entry",
            "aggregate_id": "e1",
            "occurred_at": "2026-09-20T09:30:00+08:00",
            "version": 1,
            "summary": "无停止直接恢复",
            "payload": {"authorized_by": "医疗官-01"},
        })
        self.assertTrue(any("无进行中的医疗停止" in e for e in no_case))
        stop = {
            "event_id": "med-1",
            "event_type": "MEDICAL_STOPPED",
            "aggregate_type": "competition_entry",
            "aggregate_id": "e1",
            "occurred_at": "2026-09-20T09:20:00+08:00",
            "version": 1,
            "summary": "医疗停止",
            "payload": {"reason": "处置"},
        }
        self.assertEqual(engine.ingest(stop), [])
        again = engine.ingest({**stop, "event_id": "med-2"})
        self.assertTrue(any("已处于医疗停止" in e for e in again))
        missing_by = engine.ingest({
            "event_id": "ret-2",
            "event_type": "RETURN_AUTHORIZED",
            "aggregate_type": "competition_entry",
            "aggregate_id": "e1",
            "occurred_at": "2026-09-20T09:40:00+08:00",
            "version": 1,
            "summary": "缺授权人",
            "payload": {},
        })
        self.assertTrue(any("授权人" in e for e in missing_by))

    def test_split_for_unknown_entry_rejected(self):
        engine = base_engine()
        errors = engine.ingest({
            "event_id": "sp-x",
            "event_type": "SPLIT_RECORDED",
            "aggregate_type": "split_evidence",
            "aggregate_id": "ev-x",
            "occurred_at": "2026-09-20T09:05:00+08:00",
            "version": 1,
            "summary": "幽灵记录",
            "payload": {"entry_id": "ghost", "lane": "L1", "segment_index": 0, "source": "timing_chip"},
        })
        self.assertTrue(any("报名不存在" in e for e in errors))

    def test_duplicate_event_id_rejected(self):
        engine = base_engine()
        event = entry_event(entry_id="e2")
        self.assertEqual(engine.ingest(event), [])
        self.assertTrue(any("重复事件" in e for e in engine.ingest(event)))

    def test_envelope_errors(self):
        engine = AdjudicationEngine()
        self.assertTrue(engine.ingest({"event_id": "bad"}))
        unknown = dict(format_event(), event_type="NOPE")
        self.assertTrue(any("未知事件类型" in e for e in engine.ingest(unknown)))


if __name__ == "__main__":
    unittest.main()
