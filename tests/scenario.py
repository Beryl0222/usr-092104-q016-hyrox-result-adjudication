"""测试夹具：构造 16 环节交替赛制并按真实时间喂入证据。"""

from __future__ import annotations

from datetime import datetime, timedelta

SEGMENTS = [
    ("S01", "run", "起跑跑段", 1000, None),
    ("S02", "station", "体操架", None, "rig"),
    ("S03", "run", "转场跑段", 800, None),
    ("S04", "station", "雪橇", None, "sled"),
    ("S05", "run", "负重跑段", 600, None),
    ("S06", "station", "攀绳", None, "rope"),
    ("S07", "run", "绕桩跑段", 500, None),
    ("S08", "station", "跳箱", None, "box"),
    ("S09", "run", "回程跑段", 500, None),
    ("S10", "station", "沙袋搬运架", None, "carry"),
    ("S11", "run", "短跑跑段", 400, None),
    ("S12", "station", "沙球过肩", None, "sandbag"),
    ("S13", "run", "终点跑段一", 400, None),
    ("S14", "station", "虫绳", None, "worm"),
    ("S15", "run", "终点跑段二", 300, None),
    ("S16", "station", "翻墙", None, "wall"),
]

# 跑段按 4 m/s，固定项目统一 60 秒
RUN_DURATIONS = (250, 200, 150, 125, 125, 100, 100, 75)
STATION_DURATION = 60


def segment_specs() -> list[dict]:
    specs, run_i = [], 0
    for code, kind, name, distance, apparatus in SEGMENTS:
        seg = {"code": code, "kind": kind, "name": name}
        if kind == "run":
            seg["distance_m"] = distance
            seg["duration_hint_seconds"] = RUN_DURATIONS[run_i]
            run_i += 1
        else:
            seg["apparatus_code"] = apparatus
            seg["requires_sensor"] = True
            seg["duration_hint_seconds"] = STATION_DURATION
        specs.append(seg)
    return specs


def gate_times(start: datetime, pace_scale: float = 1.0) -> dict[str, datetime]:
    """G0..G16 的计划过门时刻；pace_scale 拉伸全程节奏。"""
    durations: list[int] = []
    run_i = 0
    for _, kind, *_ in SEGMENTS:
        durations.append(RUN_DURATIONS[run_i] if kind == "run" else STATION_DURATION)
        if kind == "run":
            run_i += 1
    gates = {"G0": start}
    cur = start
    elapsed = 0.0
    for i, d in enumerate(durations):
        elapsed += d
        gates[f"G{i + 1}"] = start + timedelta(seconds=elapsed * pace_scale)
    return gates


def feed_race(svc, *, entry_id: str, chip_ids: list[str], lane: str,
              lane_devices: dict[str, str], start: datetime,
              missing_judges: tuple[str, ...] = (),
              missing_sensors: tuple[str, ...] = (),
              skip_gates: tuple[str, ...] = (),
              late_gates: dict[str, int] | None = None,
              pace_scale: float = 1.0,
              chip_lag_seconds: int = 2,
              reporter_prefix: str = "judge") -> None:
    """按真实发生顺序喂入一场比赛的全部证据。

    lane_devices: apparatus_code -> device_id（该赛道器械）。
    late_gates: {gate_code: 延迟接收秒数}，用于迟到上报场景。
    pace_scale: 全程节奏倍率，用于构造快慢不同的选手。
    """
    late_gates = late_gates or {}
    gates = gate_times(start, pace_scale)

    for gi in range(17):
        gate = f"G{gi}"
        if gate in skip_gates:
            continue
        for ci, chip_id in enumerate(chip_ids):
            observed = gates[gate] + timedelta(seconds=chip_lag_seconds * ci)
            delay = late_gates.get(gate, 5)
            svc.record_split(
                entry_id,
                source="timing_chip",
                chip_id=chip_id,
                gate_code=gate,
                observed_at=observed.isoformat(),
                received_at=(observed + timedelta(seconds=delay)).isoformat(),
                reporter="mat-01",
                event_id=f"spl-{entry_id}-{chip_id}-{gate}",
            )

    for code, kind, _, _, apparatus in SEGMENTS:
        if kind != "station":
            continue
        idx = int(code[1:]) - 1
        began, ended = gates[f"G{idx}"], gates[f"G{idx + 1}"]
        if code not in missing_judges:
            observed = began + timedelta(seconds=20)
            svc.record_split(
                entry_id,
                source="station_judge",
                segment_code=code,
                observed_at=observed.isoformat(),
                received_at=(observed + timedelta(seconds=5)).isoformat(),
                reporter=f"{reporter_prefix}-{apparatus}",
                event_id=f"spl-{entry_id}-judge-{code}",
            )
        if code not in missing_sensors and apparatus in lane_devices:
            observed = began + timedelta(seconds=40)
            svc.record_split(
                entry_id,
                source="apparatus_sensor",
                segment_code=code,
                device_id=lane_devices[apparatus],
                observed_at=observed.isoformat(),
                received_at=(observed + timedelta(seconds=5)).isoformat(),
                reporter="sensor-gw",
                event_id=f"spl-{entry_id}-sensor-{code}",
                reading={"reps": 10},
            )


def register_lane_devices(svc, lane: str, registered_at: str) -> dict[str, str]:
    """为一条赛道的 8 个固定项目登记器械。"""
    devices = {}
    for _, kind, _, _, apparatus in SEGMENTS:
        if kind != "station":
            continue
        device_id = f"dev-{apparatus}-{lane}"
        svc.register_device(device_id, apparatus_code=apparatus, lane=lane,
                            registered_at=registered_at)
        devices[apparatus] = device_id
    return devices
