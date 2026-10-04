"""端到端：相邻赛道错记、双人替换、迟到/重复、医疗、四类认定、
申诉改判递补、成绩冻结与公开验证。"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from src.app import AdjudicationError, AdjudicationService
from src.contracts import AggregateType, AnomalyKind, EventType, ReleaseStage, RulingType
from src.store import EventConflictError, EventStore
from src.validator import validate_envelope, validate_event

from tests import scenario
from tests.scenario import feed_race, gate_times, register_lane_devices, segment_specs

GUN = datetime.fromisoformat("2026-10-04T09:00:00+08:00")
FMT = "mx2026"


def build_service() -> AdjudicationService:
    svc = AdjudicationService(EventStore())
    svc.register_format(
        FMT, "万人混合体能赛", segment_specs(),
        rules_version="2026.1", wave_ids=["W1"],
        penalty_seconds={"missed_station": 90},
        occurred_at="2026-10-01T10:00:00+08:00",
    )
    return svc


def wave_and_entry(svc, entry_id, bib, *, lane="1", category="male_individual",
                   team_kind="individual", athletes=None, chip_ids=None,
                   devices=None, confirmed_at="2026-10-04T08:00:00+08:00"):
    athletes = athletes or [{"athlete_id": f"A-{bib}", "role": "athlete"}]
    chip_ids = chip_ids or [f"chip-{bib}"]
    svc.confirm_entry(
        entry_id, format_id=FMT, wave_id="W1", category=category, bib=bib,
        athletes=athletes, chip_ids=chip_ids, lane=lane, team_kind=team_kind,
        device_ids=list((devices or {}).values()), confirmed_at=confirmed_at,
    )


class EnvelopeContractTest(unittest.TestCase):
    def test_legacy_sample_still_valid(self) -> None:
        sample = json.loads(
            (Path(__file__).parents[1] / "data" / "sample.json").read_text(encoding="utf-8")
        )
        self.assertEqual(validate_event(sample), [])
        self.assertEqual(validate_envelope(sample), [])

    def test_schema_enums_match_code(self) -> None:
        schema = json.loads(
            (Path(__file__).parents[1] / "contracts" / "domain.schema.json").read_text("utf-8")
        )
        self.assertEqual(set(schema["properties"]["event_type"]["enum"]),
                         {e.value for e in EventType})
        self.assertEqual(set(schema["properties"]["aggregate_type"]["enum"]),
                         {a.value for a in AggregateType})

    def test_envelope_rejects_bad_timestamp(self) -> None:
        errors = validate_envelope({
            "event_id": "x", "event_type": "SPLIT_RECORDED",
            "aggregate_type": "competition_entry", "aggregate_id": "e",
            "occurred_at": "2026-10-04 09:00:00", "version": 1, "summary": "s",
        })
        self.assertTrue(any("时区" in e for e in errors))


class StoreTest(unittest.TestCase):
    def test_replay_same_event_id_is_idempotent_conflict_detected(self) -> None:
        store = EventStore()
        base = {
            "event_id": "e1", "event_type": "SPLIT_RECORDED",
            "aggregate_type": "competition_entry", "aggregate_id": "ent",
            "occurred_at": "2026-10-04T09:00:00+08:00", "version": 1,
            "summary": "第一次", "payload": {"v": 1},
        }
        first = store.append(dict(base))
        again = store.append(dict(base))
        self.assertIs(first, again)
        self.assertEqual(len(store.events()), 1)

        clash = dict(base, summary="篡改内容")
        with self.assertRaises(EventConflictError):
            store.append(clash)

        with self.assertRaises(Exception):
            store.append({**base, "event_id": "e2", "version": 3})

    def test_hash_chain_verifies(self) -> None:
        store = EventStore()
        for i in range(3):
            store.append({
                "event_id": f"e{i}", "event_type": "SPLIT_RECORDED",
                "aggregate_type": "competition_entry", "aggregate_id": "ent",
                "occurred_at": f"2026-10-04T09:0{i}:00+08:00", "version": i + 1,
                "summary": "s",
            })
        self.assertTrue(store.verify_chain())
        store.events()[1]["payload"]["hacked"] = True
        self.assertFalse(store.verify_chain())


class FullRaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_service()
        self.devices1 = register_lane_devices(self.svc, "1", "2026-10-04T07:30:00+08:00")
        self.svc.start_wave(FMT, "W1", GUN.isoformat())
        wave_and_entry(self.svc, "ent-A", "1001", devices=self.devices1)

    def test_sixteen_alternating_segments_assembled_by_true_time(self) -> None:
        feed_race(self.svc, entry_id="ent-A", chip_ids=["chip-1001"], lane="1",
                  lane_devices=self.devices1, start=GUN)
        assembled = self.svc.assemble_race("ent-A")
        kinds = [e["payload"]["kind"] for e in assembled]
        self.assertEqual(len(assembled), 16)
        self.assertEqual(kinds, ["run", "station"] * 8)
        # 证据基础：跑段仅计时；项目含裁判与传感器
        station = next(e for e in assembled if e["payload"]["segment_code"] == "S02")
        self.assertEqual(station["payload"]["basis"], "timing+judge+sensor")
        run = next(e for e in assembled if e["payload"]["segment_code"] == "S01")
        self.assertEqual(run["payload"]["basis"], "timing")

        # 再次组装是幂等的：不产生新版本事件
        self.assertEqual(len(self.svc.assemble_race("ent-A")), 16)
        self.assertEqual(
            len([e for e in self.svc.store.events() if e["event_type"] == EventType.SEGMENT_ASSEMBLED]),
            16,
        )

    def test_duplicate_report_does_not_add_segment(self) -> None:
        feed_race(self.svc, entry_id="ent-A", chip_ids=["chip-1001"], lane="1",
                  lane_devices=self.devices1, start=GUN)
        self.svc.assemble_race("ent-A")
        # 同一位置的第二张读数（新 event_id）判重：拒收 + 待审，环节数不变
        dup = self.svc.record_split(
            "ent-A", source="timing_chip", chip_id="chip-1001", gate_code="G5",
            observed_at=(gate_times(GUN)["G5"] + timedelta(seconds=90)).isoformat(),
            received_at=(gate_times(GUN)["G5"] + timedelta(seconds=95)).isoformat(),
            reporter="mat-01", event_id="spl-stray-G5",
        )
        self.assertEqual(dup["event_type"], EventType.EVIDENCE_REJECTED)
        referee = self.svc.referee_view(FMT)
        self.assertTrue(any(AnomalyKind.DUPLICATE in c["case_id"] for c in referee["cases"]))
        # 原 event_id 重放不多记
        before = len(self.svc.store.events())
        self.svc.record_split(
            "ent-A", source="timing_chip", chip_id="chip-1001", gate_code="G5",
            observed_at=gate_times(GUN)["G5"].isoformat(),
            received_at=(gate_times(GUN)["G5"] + timedelta(seconds=5)).isoformat(),
            reporter="mat-01", event_id="spl-ent-A-chip-1001-G5",
        )
        self.assertEqual(len(self.svc.store.events()), before)


class CrossLaneTest(unittest.TestCase):
    def test_adjacent_lane_apparatus_reading_only_goes_to_review(self) -> None:
        svc = build_service()
        d1 = register_lane_devices(svc, "1", "2026-10-04T07:30:00+08:00")
        d2 = register_lane_devices(svc, "2", "2026-10-04T07:30:00+08:00")
        svc.start_wave(FMT, "W1", GUN.isoformat())
        wave_and_entry(svc, "ent-A", "1001", lane="1", devices=d1)
        wave_and_entry(svc, "ent-B", "1002", lane="2", devices=d2)

        feed_race(svc, entry_id="ent-A", chip_ids=["chip-1001"], lane="1",
                  lane_devices=d1, start=GUN)
        # 邻道 2 号赛道的攀爬架读数被错挂到 A 名下
        g = gate_times(GUN)
        bad = svc.record_split(
            "ent-A", source="apparatus_sensor", segment_code="S02",
            device_id=d2["rig"], observed_at=(g["G1"] + timedelta(seconds=30)).isoformat(),
            received_at=(g["G1"] + timedelta(seconds=35)).isoformat(), reporter="sensor-gw",
            event_id="spl-A-wronglane-rig", reading={"reps": 999},
        )
        self.assertEqual(bad["event_type"], EventType.EVIDENCE_REJECTED)
        self.assertEqual(bad["payload"]["reason"], "cross_lane_device")

        svc.assemble_race("ent-A")
        referee = svc.referee_view(FMT)
        case = next(c for c in referee["cases"] if AnomalyKind.CROSS_LANE_DEVICE in c["case_id"])
        self.assertTrue(case["open"])
        self.assertEqual(case["anomalies"][0]["evidence"]["device_id"], d2["rig"])

        # 异常不直接产生处罚：A 的 S02 时长没有被邻道数据影响
        svc.sign_ruling(
            case["case_id"], ruling_type=RulingType.EQUIPMENT_FAULT,
            disposition="no_fault", judge_role="technical_delegate", judge_id="TD-7",
            rationale="邻道传感器串号，选手本人已由项目裁判确认完成，不罚",
            signed_at="2026-10-04T11:00:00+08:00",
        )
        case_after = next(c for c in svc.referee_view(FMT)["cases"]
                          if AnomalyKind.CROSS_LANE_DEVICE in c["case_id"])
        self.assertFalse(case_after["open"])


class PartnerReplacementTest(unittest.TestCase):
    def test_pair_partner_replacement_rules(self) -> None:
        svc = build_service()
        svc.start_wave(FMT, "W1", GUN.isoformat())
        svc.confirm_entry(
            "ent-P", format_id=FMT, wave_id="W1", category="pair_mixed",
            team_kind="pair", bib="2001", lane="3",
            athletes=[{"athlete_id": "A1", "role": "athlete"},
                      {"athlete_id": "A2", "role": "athlete"}],
            chip_ids=["chip-A1", "chip-A2"], confirmed_at="2026-10-04T08:00:00+08:00",
        )
        # 赛前、资格通过、同枪无冲突：允许一次
        svc.replace_partner(
            "ent-P", outgoing_athlete_id="A2", incoming_athlete_id="A3",
            incoming_role="athlete", qualified=True, registrar_id="REG-1",
            reason="伤病替换", decided_at="2026-10-04T08:30:00+08:00",
        )
        with self.assertRaises(AdjudicationError):  # 第二次不允许
            svc.replace_partner(
                "ent-P", outgoing_athlete_id="A1", incoming_athlete_id="A4",
                incoming_role="athlete", qualified=True, registrar_id="REG-1",
                reason="再次替换", decided_at="2026-10-04T08:40:00+08:00",
            )
        # 另一支双人组：资格不通过的替换不允许
        svc.confirm_entry(
            "ent-Q", format_id=FMT, wave_id="W1", category="pair_mixed",
            team_kind="pair", bib="2002", lane="4",
            athletes=[{"athlete_id": "B1"}, {"athlete_id": "B2"}],
            chip_ids=["c1", "c2"], confirmed_at="2026-10-04T08:00:00+08:00",
        )
        with self.assertRaises(AdjudicationError):
            svc.replace_partner(
                "ent-Q", outgoing_athlete_id="B2", incoming_athlete_id="B3",
                incoming_role="athlete", qualified=False, registrar_id="REG-1",
                reason="无资格", decided_at="2026-10-04T08:35:00+08:00",
            )
        with self.assertRaises(AdjudicationError):  # 发枪后不允许
            svc.replace_partner(
                "ent-Q", outgoing_athlete_id="B2", incoming_athlete_id="B4",
                incoming_role="athlete", qualified=True, registrar_id="REG-1",
                reason="晚了", decided_at="2026-10-04T09:30:00+08:00",
            )


class LateReportTest(unittest.TestCase):
    def test_late_evidence_reorders_by_true_time(self) -> None:
        svc = build_service()
        devices = register_lane_devices(svc, "1", "2026-10-04T07:30:00+08:00")
        svc.start_wave(FMT, "W1", GUN.isoformat())
        wave_and_entry(svc, "ent-L", "1003", devices=devices)
        # G8 先缺；组装只得到 7 个环节
        feed_race(svc, entry_id="ent-L", chip_ids=["chip-1003"], lane="1",
                  lane_devices=devices, start=GUN, skip_gates=("G8",))
        first = svc.assemble_race("ent-L")
        self.assertEqual(len(first), 7)

        # 30 分钟后 G8 才迟到上报：按真实时间归位，重排不新增环节
        g8 = gate_times(GUN)["G8"]
        ev = svc.record_split(
            "ent-L", source="timing_chip", chip_id="chip-1003", gate_code="G8",
            observed_at=g8.isoformat(),
            received_at=(g8 + timedelta(minutes=30)).isoformat(),
            reporter="mat-01", event_id="spl-ent-L-chip-1003-G8",
        )
        self.assertEqual(ev["event_type"], EventType.SPLIT_RECORDED)
        second = svc.assemble_race("ent-L")
        self.assertEqual(len(second), 16)
        case = next(c for c in svc.referee_view(FMT)["cases"] if AnomalyKind.LATE in c["case_id"])
        self.assertTrue(case["open"])


class MedicalTest(unittest.TestCase):
    def test_medical_stop_overrides_timing_and_resume_needs_other_authorizer(self) -> None:
        svc = build_service()
        svc.start_wave(FMT, "W1", GUN.isoformat())
        wave_and_entry(svc, "ent-M", "1004")
        g = gate_times(GUN)

        def chip(gate, moment):
            svc.record_split(
                "ent-M", source="timing_chip", chip_id="chip-1004", gate_code=gate,
                observed_at=moment.isoformat(),
                received_at=(moment + timedelta(seconds=5)).isoformat(),
                reporter="mat-01", event_id=f"spl-ent-M-chip-1004-{gate}",
            )

        for i in range(3):  # G0..G2 正常
            chip(f"G{i}", g[f"G{i}"])

        stop_at = g["G2"] + timedelta(seconds=10)
        hold = svc.issue_medical_stop(
            "ent-M", issued_by="MED-1", reason="擦伤处置", issued_at=stop_at.isoformat())
        hold_id = hold["aggregate_id"]

        # 停止期间的一切读数拒收
        stray = svc.record_split(
            "ent-M", source="timing_chip", chip_id="chip-1004", gate_code="G3",
            observed_at=(g["G2"] + timedelta(seconds=40)).isoformat(),
            received_at=(g["G2"] + timedelta(seconds=45)).isoformat(),
            reporter="mat-01", event_id="spl-ent-M-stray",
        )
        self.assertEqual(stray["payload"]["reason"], "medical_stop")

        with self.assertRaises(AdjudicationError):  # 不能自己批准自己恢复
            svc.authorize_resume(hold_id, authorized_by="MED-1",
                                 authorized_at=(stop_at + timedelta(seconds=120)).isoformat())
        resume_at = g["G2"] + timedelta(seconds=130)
        svc.authorize_resume(hold_id, authorized_by="MED-LEAD-2",
                             authorized_at=resume_at.isoformat())

        shift = timedelta(seconds=120)
        for i in range(3, 17):  # 恢复后 G3..G16，真实时刻整体后移 120 秒
            chip(f"G{i}", g[f"G{i}"] + shift)

        svc.assemble_race("ent-M")
        view = svc.participant_view("ent-M")
        planned_total = sum(
            s["duration_hint_seconds"] for s in segment_specs())
        self.assertEqual(view["medical_seconds"], 120.0)
        self.assertEqual(view["final_seconds"], planned_total + 120 - 120)
        self.assertEqual(view["gross_seconds"], planned_total + 120)


class RulingAuthorityTest(unittest.TestCase):
    def _open_case(self, svc, entry_id="ent-R", bib="1005", missing=("S06",)):
        devices = register_lane_devices(svc, "1", "2026-10-04T07:30:00+08:00")
        svc.start_wave(FMT, "W1", GUN.isoformat())
        wave_and_entry(svc, entry_id, bib, devices=devices)
        feed_race(svc, entry_id=entry_id, chip_ids=[f"chip-{bib}"], lane="1",
                  lane_devices=devices, start=GUN, missing_judges=missing)
        svc.assemble_race(entry_id)
        return next(c["case_id"] for c in svc.referee_view(FMT)["cases"]
                    if AnomalyKind.MISSED_STATION in c["case_id"])

    def test_four_ruling_types_each_need_their_referee(self) -> None:
        svc = build_service()
        case_id = self._open_case(svc)
        # 漏站只能由站点裁判认定
        with self.assertRaises(AdjudicationError):
            svc.sign_ruling(case_id, ruling_type=RulingType.MISSED_STATION,
                            disposition="time_penalty", judge_role="technical_delegate",
                            judge_id="TD-1", rationale="x",
                            signed_at="2026-10-04T11:00:00+08:00", penalty_seconds=90)
        svc.sign_ruling(case_id, ruling_type=RulingType.MISSED_STATION,
                        disposition="time_penalty", judge_role="station_referee",
                        judge_id="SR-1", rationale="S06 漏站，按规则加 90 秒",
                        signed_at="2026-10-04T11:00:00+08:00", penalty_seconds=90)

    def test_equipment_fault_and_misdirection_cannot_penalize(self) -> None:
        svc = build_service()
        case_id = self._open_case(svc, entry_id="ent-R2", bib="1006")
        with self.assertRaises(AdjudicationError):
            svc.sign_ruling(case_id, ruling_type=RulingType.EQUIPMENT_FAULT,
                            disposition="time_penalty", judge_role="technical_delegate",
                            judge_id="TD-1", rationale="设备锅但罚运动员",
                            signed_at="2026-10-04T11:00:00+08:00", penalty_seconds=30)
        with self.assertRaises(AdjudicationError):
            svc.sign_ruling(case_id, ruling_type=RulingType.VOLUNTEER_MISDIRECTION,
                            disposition="disqualification", judge_role="chief_course_judge",
                            judge_id="CCJ-1", rationale="志愿者误导却取消资格",
                            signed_at="2026-10-04T11:00:00+08:00")
        # 志愿者误导只能修正计时
        svc.sign_ruling(case_id, ruling_type=RulingType.VOLUNTEER_MISDIRECTION,
                        disposition="adjusted_time", judge_role="chief_course_judge",
                        judge_id="CCJ-1", rationale="志愿者错误指引多跑 40 秒，予以修正",
                        signed_at="2026-10-04T11:00:00+08:00", adjustment_seconds=-40)

    def test_athlete_violation_dq_needs_jury(self) -> None:
        svc = build_service()
        case_id = self._open_case(svc, entry_id="ent-R3", bib="1007")
        with self.assertRaises(AdjudicationError):
            svc.sign_ruling(case_id, ruling_type=RulingType.ATHLETE_VIOLATION,
                            disposition="disqualification", judge_role="station_referee",
                            judge_id="SR-9", rationale="抄近道",
                            signed_at="2026-10-04T11:00:00+08:00")
        svc.sign_ruling(case_id, ruling_type=RulingType.ATHLETE_VIOLATION,
                        disposition="disqualification", judge_role="competition_jury",
                        judge_id="JURY-1", rationale="抄近道，取消资格",
                        signed_at="2026-10-04T11:00:00+08:00")
        self.assertEqual(svc.participant_view("ent-R3")["status"], "disqualified")


class AppealAndRerollTest(unittest.TestCase):
    def test_preliminary_appeal_republish_rerolls_awards_and_certificates(self) -> None:
        svc = build_service()
        devices = register_lane_devices(svc, "1", "2026-10-04T07:30:00+08:00")
        devices5 = register_lane_devices(svc, "5", "2026-10-04T07:30:00+08:00")
        svc.start_wave(FMT, "W1", GUN.isoformat())
        # A 更快（基准节奏），B 全程慢 2%
        wave_and_entry(svc, "ent-A", "1001", devices=devices)
        feed_race(svc, entry_id="ent-A", chip_ids=["chip-1001"], lane="1",
                  lane_devices=devices, start=GUN, missing_judges=("S06",))
        wave_and_entry(svc, "ent-B", "1002", lane="5", devices=devices5)
        feed_race(svc, entry_id="ent-B", chip_ids=["chip-1002"], lane="5",
                  lane_devices=devices5, start=GUN, pace_scale=1.02)
        svc.assemble_race("ent-A")
        svc.assemble_race("ent-B")

        # 初榜：A 第一（S06 待审未决，但不直接罚）
        svc.publish_results(FMT, stage=ReleaseStage.PRELIMINARY, published_by="TD-1",
                            published_at="2026-10-04T11:30:00+08:00")
        pub = svc.public_results(FMT)
        top = {r["entry_id"]: r["rank"] for r in pub["rankings"]}
        self.assertEqual(top["ent-A"], 1)
        self.assertEqual(top["ent-B"], 2)

        # 认定漏站加 90 秒 → B 反超
        case_id = next(c["case_id"] for c in svc.referee_view(FMT)["cases"]
                       if AnomalyKind.MISSED_STATION in c["case_id"])
        svc.sign_ruling(case_id, ruling_type=RulingType.MISSED_STATION,
                        disposition="time_penalty", judge_role="station_referee",
                        judge_id="SR-1", rationale="S06 漏站加 90 秒",
                        signed_at="2026-10-04T11:40:00+08:00", penalty_seconds=90)
        svc.publish_results(FMT, stage=ReleaseStage.PRELIMINARY, published_by="TD-1",
                            published_at="2026-10-04T11:45:00+08:00")
        top = {r["entry_id"]: r["rank"] for r in svc.public_results(FMT)["rankings"]}
        self.assertEqual(top["ent-B"], 1)
        self.assertEqual(top["ent-A"], 2)

        # A 申诉：处罚理由对参赛者可见
        view = svc.participant_view("ent-A")
        self.assertEqual(view["penalties"][0]["rationale"], "S06 漏站加 90 秒")
        appeal = svc.file_appeal(
            "ent-A", view["penalties"][0]["ruling_id"],
            grounds="有视频证明完成 S06，系裁判站漏勾", filed_by="A-1001",
            filed_at="2026-10-04T12:00:00+08:00")
        appeal_id = appeal["aggregate_id"]

        # 申诉期间只能发布申诉中状态
        svc.publish_results(FMT, stage=ReleaseStage.UNDER_APPEAL, published_by="TD-1",
                            published_at="2026-10-04T12:05:00+08:00")
        self.assertEqual(svc.public_results(FMT)["stage"], ReleaseStage.UNDER_APPEAL)
        with self.assertRaises(AdjudicationError):
            svc.lock_results(FMT, locked_by="TD-1", locked_at="2026-10-04T12:06:00+08:00")

        # 改判：站点裁判复核后改为不罚（新认定取代旧认定）
        old_ruling = view["penalties"][0]["ruling_id"]
        new_ruling = svc.sign_ruling(
            case_id, ruling_type=RulingType.MISSED_STATION, disposition="no_fault",
            judge_role="station_referee", judge_id="SR-2",
            rationale="视频与传感器均证实完成 S06，撤销加时",
            signed_at="2026-10-04T12:30:00+08:00", supersedes_ruling_id=old_ruling)
        svc.decide_appeal(appeal_id, outcome="upheld", decided_by="JURY-LEAD",
                          decided_at="2026-10-04T12:35:00+08:00",
                          new_ruling_id=new_ruling["aggregate_id"])

        # 新版本成绩：A 重回第一，奖励递补，证书按版本重发
        svc.publish_results(FMT, stage=ReleaseStage.PRELIMINARY, published_by="TD-1",
                            published_at="2026-10-04T12:40:00+08:00")
        top = {r["entry_id"]: r["rank"] for r in svc.public_results(FMT)["rankings"]}
        self.assertEqual(top["ent-A"], 1)

        awards = [e for e in svc.store.events() if e["event_type"] == EventType.AWARD_REROLLED]
        rerolls = [e for e in awards if "previous_entry_id" in e["payload"]
                   and e["payload"]["place"] == 1 and e["payload"]["category"] == "male_individual"]
        self.assertEqual(rerolls[-1]["payload"]["previous_entry_id"], "ent-B")
        self.assertEqual(rerolls[-1]["payload"]["entry_id"], "ent-A")
        certs = [e for e in svc.store.events() if e["event_type"] == EventType.CERTIFICATE_ISSUED]
        # v1/v2/v4 发证书；v3 为申诉中状态快照，不发证书
        self.assertEqual({e["payload"]["release_version"] for e in certs}, {1, 2, 4})
        self.assertTrue(any(e["payload"]["rank"] == 1 and e["payload"]["release_version"] == 4
                            for e in certs if e["payload"]["entry_id"] == "ent-A"))

        # 全部无未决事项 → 正式锁定，公开结果可验证
        svc.lock_results(FMT, locked_by="TD-1", locked_at="2026-10-04T13:00:00+08:00")
        certs_after = [e for e in svc.store.events()
                       if e["event_type"] == EventType.CERTIFICATE_ISSUED]
        self.assertEqual({e["payload"]["release_version"] for e in certs_after}, {1, 2, 4, 5})
        public = svc.public_results(FMT)
        self.assertEqual(public["stage"], ReleaseStage.OFFICIAL)
        self.assertTrue(public["chain_intact"])
        verified = svc.verify_latest_release(FMT)
        self.assertTrue(verified["snapshot_digest_matches"])

        # 冻结资料中包含赛制、分枪、搭档资格、器械状态
        latest = svc._snapshots().releases.latest(FMT)
        freeze = latest["freeze"]
        self.assertEqual(len(freeze["rules"]["segments"]), 16)
        self.assertRegex(freeze["rules"]["segments_digest"], r"^[0-9a-f]{64}$")
        self.assertIn("W1", freeze["waves"])
        self.assertIn("ent-A", freeze["entries"])
        self.assertTrue(all(d["status"] == "nominal" for d in freeze["devices"].values()))

        # 初榜/申诉中/正式三个阶段在历史中都保留
        stages = [r["stage"] for r in svc._snapshots().releases.history(FMT)]
        self.assertEqual(stages, [ReleaseStage.PRELIMINARY, ReleaseStage.PRELIMINARY,
                                  ReleaseStage.UNDER_APPEAL, ReleaseStage.PRELIMINARY,
                                  ReleaseStage.OFFICIAL])


class FreezePartnerTest(unittest.TestCase):
    def test_partner_qualification_frozen_into_release(self) -> None:
        svc = build_service()
        devices = register_lane_devices(svc, "3", "2026-10-04T07:30:00+08:00")
        svc.start_wave(FMT, "W1", GUN.isoformat())
        svc.confirm_entry(
            "ent-P", format_id=FMT, wave_id="W1", category="pair_mixed", team_kind="pair",
            bib="2001", lane="3",
            athletes=[{"athlete_id": "A1", "role": "athlete"},
                      {"athlete_id": "A2", "role": "athlete"}],
            chip_ids=["chip-A1", "chip-A2"], device_ids=list(devices.values()),
            confirmed_at="2026-10-04T08:00:00+08:00",
        )
        svc.replace_partner(
            "ent-P", outgoing_athlete_id="A2", incoming_athlete_id="A3",
            incoming_role="athlete", qualified=True, registrar_id="REG-1",
            reason="伤病替换", decided_at="2026-10-04T08:30:00+08:00")
        feed_race(svc, entry_id="ent-P", chip_ids=["chip-A1", "chip-A2"], lane="3",
                  lane_devices=devices, start=GUN)
        svc.assemble_race("ent-P")
        svc.publish_results(FMT, stage=ReleaseStage.PRELIMINARY, published_by="TD-1",
                            published_at="2026-10-04T11:30:00+08:00")
        frozen = svc._snapshots().releases.latest(FMT)["freeze"]["entries"]["ent-P"]
        self.assertEqual(frozen["partner_replacements"][0]["incoming_athlete_id"], "A3")
        self.assertEqual({a["athlete_id"] for a in frozen["athletes"]}, {"A1", "A3"})


class DeviceFaultWindowTest(unittest.TestCase):
    def test_reading_during_fault_goes_to_review_not_score(self) -> None:
        svc = build_service()
        devices = register_lane_devices(svc, "1", "2026-10-04T07:30:00+08:00")
        svc.start_wave(FMT, "W1", GUN.isoformat())
        wave_and_entry(svc, "ent-D", "1008", devices=devices)
        g = gate_times(GUN)
        # S04 雪橇器械（G3..G4 之间）开赛后故障，赛后恢复
        sled = devices["sled"]
        svc.record_device_status(sled, "fault", note="测力模块离线",
                                 recorded_at=(g["G3"] + timedelta(seconds=5)).isoformat())
        svc.record_device_status(sled, "nominal", note="模块复位",
                                 recorded_at=(g["G4"] + timedelta(minutes=5)).isoformat())
        feed_race(svc, entry_id="ent-D", chip_ids=["chip-1008"], lane="1",
                  lane_devices=devices, start=GUN)
        # 故障窗口内的传感器读数拒收并进待审（feed 中的 S04 传感器恰在窗口内）
        rejected = [r for r in svc._snapshots().races.for_entry("ent-D")["rejected"]
                    if r.get("segment_code") == "S04"]
        self.assertTrue(rejected)
        self.assertEqual(rejected[0]["reason"], "device_not_nominal")

        svc.assemble_race("ent-D")
        case = next(c for c in svc.referee_view(FMT)["cases"]
                    if c["case_id"].endswith("s04") and any(
                        a["kind"] == AnomalyKind.DEVICE_SUSPECT for a in c["anomalies"]))
        # 技术代表认定设备故障 → 只能修正计时/不罚，不能处罚运动员
        with self.assertRaises(AdjudicationError):
            svc.sign_ruling(case["case_id"], ruling_type=RulingType.EQUIPMENT_FAULT,
                            disposition="time_penalty", judge_role="technical_delegate",
                            judge_id="TD-1", rationale="x",
                            signed_at="2026-10-04T11:00:00+08:00", penalty_seconds=30)
        svc.sign_ruling(case["case_id"], ruling_type=RulingType.EQUIPMENT_FAULT,
                        disposition="adjusted_time", judge_role="technical_delegate",
                        judge_id="TD-1", rationale="故障损失 15 秒予以补偿",
                        signed_at="2026-10-04T11:00:00+08:00", adjustment_seconds=-15)
        view = svc.participant_view("ent-D")
        self.assertEqual(view["adjustment_seconds"], -15)
        self.assertEqual(view["penalty_seconds"], 0)


class JournalReplayTest(unittest.TestCase):
    def test_state_rebuilds_from_journal_and_replay_is_idempotent(self) -> None:
        import tempfile
        from src.store import EventStore as ES

        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "events.jsonl"
            svc1 = AdjudicationService(ES(journal))
            svc1.register_format(FMT, "万人混合体能赛", segment_specs(),
                                 rules_version="2026.1", wave_ids=["W1"],
                                 occurred_at="2026-10-01T10:00:00+08:00")
            devices = register_lane_devices(svc1, "1", "2026-10-04T07:30:00+08:00")
            svc1.start_wave(FMT, "W1", GUN.isoformat())
            wave_and_entry(svc1, "ent-A", "1001", devices=devices)
            feed_race(svc1, entry_id="ent-A", chip_ids=["chip-1001"], lane="1",
                      lane_devices=devices, start=GUN)
            svc1.assemble_race("ent-A")
            head1 = svc1.store.head_hash
            count1 = len(svc1.store.events())

            # 用同一日志重新打开：状态完整重建，链摘要一致
            svc2 = AdjudicationService(ES(journal))
            self.assertEqual(svc2.store.head_hash, head1)
            self.assertEqual(len(svc2.store.events()), count1)
            self.assertEqual(len(svc2.assemble_race("ent-A")), 16)
            # 重放接入不产生新事件
            self.assertEqual(len(svc2.store.events()), count1)


if __name__ == "__main__":
    unittest.main()
