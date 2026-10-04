"""生成 data/sample_events.json：一条完整的联调事件流。

场景：16 个交替环节的混合体能赛。
- e1（混双，L3）赛前换搭档；比赛中被计入相邻 L4 赛道的器械传感器记录；
  另有重复上报与迟到上报各一例。
- e2（混双，L4）第 9 环节的器械记录被串到 e1 名下，初榜时环节缺失。
- e3（个人，L5）赛中医疗停止，授权恢复后完赛；因志愿者误导获时间减免。
- 初榜 -> 申诉中 -> 正式 -> 视频复核改判 -> 新正式版本，奖励递补。
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
WAVE = datetime(2026, 9, 20, 9, 0, 0, tzinfo=TZ)


def at(minutes, seconds=0):
    return (WAVE + timedelta(minutes=minutes, seconds=seconds)).isoformat()


events = []
_seq = 0


def ev(event_type, aggregate_type, aggregate_id, occurred_at, version, summary, payload=None, event_id=None):
    global _seq
    _seq += 1
    item = {
        "event_id": event_id or f"evt-{_seq:03d}",
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": occurred_at,
        "version": version,
        "summary": summary,
    }
    if payload is not None:
        item["payload"] = payload
    events.append(item)


def split(evid, entry, lane, seg, occurred, seq_no, reported=None, metrics=None,
          apparatus_id=None, source=None):
    payload = {
        "entry_id": entry,
        "lane": lane,
        "segment_index": seg,
        "source": source or ("timing_chip" if seg % 2 == 0 else "station_referee"),
        "device_seq": seq_no,
    }
    if reported:
        payload["reported_at"] = reported
    if metrics:
        payload["metrics"] = metrics
    if apparatus_id:
        payload["apparatus_id"] = apparatus_id
    ev("SPLIT_RECORDED", "split_evidence", evid, occurred, 1,
       f"{entry} 第{seg}环节记录", payload, event_id=f"evt-{evid}")


# 1. 赛制：16 个交替环节（跑段/固定项目），两组一枪。
segments = [
    {"index": i, "kind": "run" if i % 2 == 0 else "station",
     "station_id": None if i % 2 == 0 else f"ST{(i + 1) // 2}"}
    for i in range(16)
]
ev("FORMAT_PUBLISHED", "race_format", "fmt-hybrid-16", "2026-09-19T10:00:00+08:00", 1,
   "发布赛制：16 个交替环节", {
       "segments": segments,
       "groups": ["混双组", "个人组"],
       "waves": [{"wave_id": "W1", "starts_at": "2026-09-20T09:00:00+08:00"}],
       "rules": {"late_report_grace_seconds": 120, "award_places": 1},
   })

# 2. 器械状态登记。
for i, lane in enumerate(("L3", "L4", "L5")):
    ev("APPARATUS_STATUS_CHANGED", "apparatus", f"AP-{lane}",
       f"2026-09-19T08:0{i}:00+08:00", 1, f"{lane} 赛道器械就绪",
       {"lane": lane, "status": "ok"})

# 3. 报名确认；e1 赛前更换搭档（李华伤退，王芳替补）。
def entry(entry_id, version, occurred, group, lane, athletes, eligibility):
    ev("ENTRY_CONFIRMED", "competition_entry", entry_id, occurred, version,
       f"确认报名 {entry_id} v{version}", {
           "format_id": "fmt-hybrid-16",
           "group": group,
           "wave": "W1",
           "lane": lane,
           "athlete_ids": athletes,
           "partner_eligibility": eligibility,
       })

entry("e1", 1, "2026-09-19T12:00:00+08:00", "混双组", "L3", ["张明", "李华"],
      {"rule": "同组配对", "eligible": True, "checked_by": "资格审查-02"})
entry("e2", 1, "2026-09-19T12:05:00+08:00", "混双组", "L4", ["陈强", "刘洋"],
      {"rule": "同组配对", "eligible": True, "checked_by": "资格审查-02"})
entry("e3", 1, "2026-09-19T12:10:00+08:00", "个人组", "L5", ["赵敏"],
      {"rule": "个人参赛", "eligible": True, "checked_by": "资格审查-02"})
entry("e1", 2, "2026-09-20T08:30:00+08:00", "混双组", "L3", ["张明", "王芳"],
      {"rule": "赛前更换需医疗证明", "eligible": True, "checked_by": "资格审查-02",
       "note": "李华伤退，王芳替补"})

# 4. e1 的 16 个环节：第 3 环节重复上报一次，第 5 环节迟到上报，
#    第 9 环节混入一条相邻 L4 赛道的器械传感器记录。
for i in range(16):
    occurred = at(5 * (i + 1))
    reported = None
    if i == 5:
        reported = (WAVE + timedelta(minutes=30, seconds=600)).isoformat()
    split(f"e1-s{i}", "e1", "L3", i, occurred, f"e1-dev-{i}", reported=reported)
    if i == 3:
        split("e1-s3-dup", "e1", "L3", 3, occurred, "e1-dev-3",
              reported=(WAVE + timedelta(minutes=20, seconds=2)).isoformat())
    if i == 9:
        split("ev-stray-l4", "e1", "L4", 9, at(50, 40), "AP4-0092",
              metrics={"reps": 30}, apparatus_id="AP-L4", source="apparatus_sensor")

# 5. e2 的环节：第 9 环节的器械记录被串到 e1 名下，本侧缺失。
for i in range(16):
    if i == 9:
        continue
    split(f"e2-s{i}", "e2", "L4", i, at(5 * (i + 1), 40), f"e2-dev-{i}")

# 6. e3 前 8 个环节后医疗停止。
for i in range(8):
    split(f"e3-s{i}", "e3", "L5", i, at(5 * (i + 1)), f"e3-dev-{i}")
ev("MEDICAL_STOPPED", "competition_entry", "e3", at(40, 30), 1,
   "e3 医疗停止", {"reason": "赛道医疗点处置"})

# 7. 初榜：e1/e2 有异常待审，e3 医疗停止。
ev("RESULT_REPUBLISHED", "result_release", "rel-2026", at(90), 1,
   "初榜发布", {"format_id": "fmt-hybrid-16", "status": "initial"})

# 8. 器械裁判认定串道，记录改属 e2；e2 漏站异常随证据归位自动解除。
ev("RULING_SIGNED", "ruling", "r1", at(95), 1, "串道记录改属 e2", {
    "category": "equipment_failure",
    "referee_id": "EQ-07",
    "referee_role": "equipment_referee",
    "entry_id": "e1",
    "anomaly_ids": ["ANX-1"],
    "outcome": "reassign_evidence",
    "reassign_to": "e2",
    "reason": "器械传感器串道，记录实际归属 L4 赛道",
})

# 9. 申诉中版本：e1、e2 上榜，e3 仍医疗停止。
ev("RESULT_REPUBLISHED", "result_release", "rel-2026", at(100), 2,
   "申诉中发布", {"format_id": "fmt-hybrid-16", "status": "appeal_pending"})

# 10. e3 经授权恢复参赛并完成后 8 个环节。
ev("RETURN_AUTHORIZED", "competition_entry", "e3", at(105), 1,
   "e3 恢复参赛", {"authorized_by": "医疗官-07"})
for i in range(8, 16):
    split(f"e3-s{i}", "e3", "L5", i, at(110 + 5 * (i - 8)), f"e3-dev-{i}")

# 11. 赛道裁判认定志愿者误导，e3 获 60 秒减免。
ev("ANOMALY_FLAGGED", "competition_entry", "e3", at(150), 1,
   "外部报告：志愿者指引问题", {
       "anomaly_id": "EXT-1",
       "entry_id": "e3",
       "kind": "external_report",
       "detail": "志愿者指引错误导致绕行",
       "evidence_ids": [],
   }, event_id="evt-ext-1")
ev("RULING_SIGNED", "ruling", "r2", at(155), 1, "志愿者误导减免", {
    "category": "volunteer_misdirection",
    "referee_id": "CR-03",
    "referee_role": "course_referee",
    "entry_id": "e3",
    "anomaly_ids": ["EXT-1"],
    "outcome": "time_adjustment",
    "credit_seconds": 60,
    "reason": "志愿者误导绕行，减免60秒",
})

# 12. 正式成绩。
ev("RESULT_REPUBLISHED", "result_release", "rel-2026", at(160), 3,
   "正式成绩发布", {"format_id": "fmt-hybrid-16", "status": "official"})

# 13. 视频复核改判：e1 器械区违规加罚 60 秒，以新正式版本完成递补。
ev("RULING_SIGNED", "ruling", "r3", at(170), 1, "视频复核处罚", {
    "category": "athlete_violation",
    "referee_id": "CR-11",
    "referee_role": "competition_referee",
    "entry_id": "e1",
    "anomaly_ids": [],
    "outcome": "penalty",
    "penalty_seconds": 60,
    "reason": "视频复核确认器械区违规，加罚60秒",
})
ev("RESULT_REPUBLISHED", "result_release", "rel-2026", at(175), 4,
   "改判后正式成绩", {"format_id": "fmt-hybrid-16", "status": "official"})

out = Path(__file__).parents[1] / "data" / "sample_events.json"
out.write_text(json.dumps(events, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"已生成 {out}，共 {len(events)} 条事件")
