"""完赛裁决引擎：按仓库信封事件驱动，维护证据、异常、认定、医疗与成绩版本。

设计要点：
- 接入事件一律先过 src/envelope 的信封校验，事件 id 幂等去重。
- 计时芯片、项目裁判、器械传感器记录按 occurred_at（真实发生时间）合成环节；
  迟到与重复上报不多算环节，校验异常只进入待审。
- 漏站、设备故障、志愿者误导、运动员违规只能由有权裁判签署认定。
- 医疗停止高于计时流程；恢复参赛必须凭 RETURN_AUTHORIZED 授权。
- 初榜、申诉中、正式成绩均为不可改写的发布版本；改判只产生新版本，
  名次、证书与奖励递补随新版本重算。
- 每份成绩冻结赛制版本、组别、分枪、搭档资格与器械状态；公开结果携带
  sha256 摘要与前序摘要链，任何一方可重算验证。
"""
from __future__ import annotations

from copy import deepcopy

from src.assembly import AssemblyBook
from src.envelope import validate_envelope
from src.model import (
    ANOMALY_KINDS,
    ANOMALY_PENDING,
    ANOMALY_RESOLVED,
    APPARATUS_STATUSES,
    EVIDENCE_ACCEPTED,
    EVIDENCE_QUARANTINED,
    EVIDENCE_REASSIGNED,
    EVIDENCE_EXCLUDED,
    EVIDENCE_SOURCES,
    RELEASE_STATUSES,
    RULING_AUTHORITY,
    RULING_CATEGORIES,
    RULING_OUTCOMES,
    SEGMENT_KINDS,
    STANDING_INCOMPLETE,
    STANDING_MEDICAL,
    STANDING_PENDING,
    STANDING_RANKED,
    Anomaly,
    ApparatusStatus,
    AppliedPenalty,
    Certificate,
    Entry,
    EntryResult,
    Evidence,
    FreezeSnapshot,
    MedicalCase,
    PenaltyView,
    RaceFormat,
    Release,
    Ruling,
    Segment,
    SplitView,
    digest_of,
    parse_ts,
    release_payload,
    release_signing_payload,
    result_signing_payload,
    verify_release_payload,
    freeze_payload,
    penalty_payload,
    split_view_payload,
)

# 一次性事件（不可变记录）的信封版本必须为 1。
_SINGLE_VERSION_EVENTS = (
    "SPLIT_RECORDED",
    "ANOMALY_FLAGGED",
    "RULING_SIGNED",
    "APPARATUS_STATUS_CHANGED",
    "MEDICAL_STOPPED",
    "RETURN_AUTHORIZED",
)


def _require(payload: dict, keys: tuple[str, ...]) -> list[str]:
    return [f"payload 缺少字段：{k}" for k in keys if k not in payload]


class AdjudicationEngine:
    def __init__(self) -> None:
        self.formats: dict[str, RaceFormat] = {}
        self.entries: dict[str, Entry] = {}
        self.apparatus: dict[str, ApparatusStatus] = {}
        self.evidence: dict[str, Evidence] = {}
        self.anomalies: dict[str, Anomaly] = {}
        self.rulings: dict[str, Ruling] = {}
        self.medical_cases: dict[str, MedicalCase] = {}
        self.releases: list[Release] = []
        self.certificates: dict[str, Certificate] = {}
        self.event_log: list[dict] = []
        self._event_ids: set[str] = set()
        self._anomaly_seq = 0
        self._books: dict[str, AssemblyBook] = {}
        self._handlers = {
            "FORMAT_PUBLISHED": self._format_published,
            "ENTRY_CONFIRMED": self._entry_confirmed,
            "SPLIT_RECORDED": self._split_recorded,
            "ANOMALY_FLAGGED": self._anomaly_flagged,
            "RULING_SIGNED": self._ruling_signed,
            "RESULT_REPUBLISHED": self._result_republished,
            "APPARATUS_STATUS_CHANGED": self._apparatus_status_changed,
            "MEDICAL_STOPPED": self._medical_stopped,
            "RETURN_AUTHORIZED": self._return_authorized,
        }

    # ---- 事件接入 ----

    def ingest(self, event: dict) -> list[str]:
        """接入一条信封事件；返回错误列表（空为成功），失败不产生任何状态变化。"""
        errors = validate_envelope(event)
        if errors:
            return errors
        if event["event_id"] in self._event_ids:
            return [f"重复事件：{event['event_id']}"]
        if event["event_type"] in _SINGLE_VERSION_EVENTS and event["version"] != 1:
            return [f"{event['event_type']} 为一次性事件，version 必须为 1"]
        errors = self._handlers[event["event_type"]](event)
        if errors:
            return errors
        self._event_ids.add(event["event_id"])
        self.event_log.append(event)
        return []

    # ---- 赛制与报名 ----

    def _format_published(self, event: dict) -> list[str]:
        p = event.get("payload") or {}
        errors = _require(p, ("segments", "groups", "waves"))
        if errors:
            return errors
        format_id = event["aggregate_id"]
        existing = self.formats.get(format_id)
        if existing is None:
            if event["version"] != 1:
                return ["赛制首版版本号必须为 1"]
        else:
            if event["version"] != existing.version + 1:
                return ["赛制版本必须递增"]
            if any(e.format_id == format_id for e in self.entries.values()):
                return ["已有报名引用该赛制，赛制不可变更"]
        segments: list[Segment] = []
        seen: set[int] = set()
        for raw in p["segments"]:
            index, kind = raw.get("index"), raw.get("kind")
            if not isinstance(index, int) or index in seen:
                return ["环节序号必须为唯一整数"]
            if kind not in SEGMENT_KINDS:
                return [f"未知环节类型：{kind}"]
            seen.add(index)
            segments.append(Segment(index, kind, raw.get("station_id")))
        if sorted(seen) != list(range(len(segments))):
            return ["环节序号必须从 0 连续编号"]
        segments.sort(key=lambda s: s.index)
        try:
            waves = {w["wave_id"]: parse_ts(w["starts_at"]) for w in p["waves"]}
        except (KeyError, ValueError) as exc:
            return [f"分枪定义不合法：{exc}"]
        rules = deepcopy(p.get("rules", {}))
        self.formats[format_id] = RaceFormat(
            format_id, event["version"], segments, list(p["groups"]), waves, rules
        )
        self._books[format_id] = AssemblyBook(
            len(segments), int(rules.get("late_report_grace_seconds", 300))
        )
        return []

    def _entry_confirmed(self, event: dict) -> list[str]:
        p = event.get("payload") or {}
        errors = _require(
            p, ("format_id", "group", "wave", "lane", "athlete_ids", "partner_eligibility")
        )
        if errors:
            return errors
        fmt = self.formats.get(p["format_id"])
        if fmt is None:
            return [f"赛制不存在：{p['format_id']}"]
        if p["group"] not in fmt.groups:
            return [f"组别不在赛制内：{p['group']}"]
        if p["wave"] not in fmt.waves:
            return [f"分枪不在赛制内：{p['wave']}"]
        if not p["athlete_ids"]:
            return ["运动员名单不能为空"]
        entry_id = event["aggregate_id"]
        occurred = parse_ts(event["occurred_at"])
        entry = self.entries.get(entry_id)
        if entry is None:
            if event["version"] != 1:
                return ["报名首版版本号必须为 1"]
            self.entries[entry_id] = Entry(
                entry_id,
                p["format_id"],
                1,
                tuple(p["athlete_ids"]),
                len(p["athlete_ids"]) > 1,
                p["group"],
                p["wave"],
                p["lane"],
                dict(p["partner_eligibility"]),
                occurred,
            )
        else:
            if event["version"] != entry.version + 1:
                return ["报名版本必须递增"]
            for fixed in ("format_id", "group", "wave", "lane"):
                if p[fixed] != getattr(entry, fixed):
                    return [f"{fixed} 不可变更"]
            # 赛前替换搭档是合法的新版本；出发后变更进入待审。
            if tuple(p["athlete_ids"]) != entry.athlete_ids and occurred > fmt.waves[entry.wave]:
                self._raise_anomaly(
                    entry_id, "late_partner_change", "分枪出发后变更搭档", (), occurred, None
                )
            entry.version = event["version"]
            entry.athlete_ids = tuple(p["athlete_ids"])
            entry.partner_eligibility = dict(p["partner_eligibility"])
            entry.confirmed_at = occurred
        entry = self.entries[entry_id]
        if entry.partner_eligibility.get("eligible") is False:
            self._raise_anomaly(
                entry_id, "partner_ineligible", "搭档资格未通过审查", (), occurred, None
            )
        return []

    def _apparatus_status_changed(self, event: dict) -> list[str]:
        p = event.get("payload") or {}
        errors = _require(p, ("lane", "status"))
        if errors:
            return errors
        if p["status"] not in APPARATUS_STATUSES:
            return [f"未知器械状态：{p['status']}"]
        self.apparatus[event["aggregate_id"]] = ApparatusStatus(
            event["aggregate_id"],
            p["lane"],
            p["status"],
            parse_ts(event["occurred_at"]),
            p.get("note", ""),
        )
        return []

    # ---- 证据合成 ----

    def _split_recorded(self, event: dict) -> list[str]:
        p = event.get("payload") or {}
        errors = _require(p, ("entry_id", "lane", "segment_index", "source"))
        if errors:
            return errors
        entry = self.entries.get(p["entry_id"])
        if entry is None:
            return [f"报名不存在：{p['entry_id']}"]
        if p["source"] not in EVIDENCE_SOURCES:
            return [f"未知证据来源：{p['source']}"]
        if not isinstance(p["segment_index"], int):
            return ["segment_index 必须为整数"]
        occurred = parse_ts(event["occurred_at"])
        reported = parse_ts(p.get("reported_at", event["occurred_at"]))
        if reported < occurred:
            return ["上报时间早于发生时间"]
        ev = Evidence(
            event["aggregate_id"],
            entry.entry_id,
            p["lane"],
            p["segment_index"],
            p["source"],
            occurred,
            reported,
            p.get("device_seq"),
            dict(p.get("metrics", {})),
        )
        self.evidence[ev.evidence_id] = ev
        # 器械状态异常期间的传感器记录先隔离待审，不计入成绩。
        if ev.source == "apparatus_sensor" and p.get("apparatus_id"):
            apparatus = self.apparatus.get(p["apparatus_id"])
            if apparatus is not None and apparatus.status != "ok":
                ev.status = EVIDENCE_QUARANTINED
                self._raise_anomaly(
                    entry.entry_id,
                    "apparatus_fault",
                    f"器械 {apparatus.apparatus_id} 状态 {apparatus.status}，记录待审",
                    (ev.evidence_id,),
                    occurred,
                    ev.segment_index,
                )
                return []
        book = self._books[entry.format_id]
        drafts = book.ingest(ev, entry.lane)
        self._raise_drafts(entry.entry_id, drafts, occurred)
        self._auto_resolve_gaps(entry.entry_id)
        return []

    def _anomaly_flagged(self, event: dict) -> list[str]:
        p = event.get("payload") or {}
        errors = _require(p, ("anomaly_id", "entry_id", "kind", "detail"))
        if errors:
            return errors
        if p["kind"] not in ANOMALY_KINDS:
            return [f"未知异常类型：{p['kind']}"]
        if p["entry_id"] not in self.entries:
            return [f"报名不存在：{p['entry_id']}"]
        if p["anomaly_id"] in self.anomalies:
            return [f"异常编号重复：{p['anomaly_id']}"]
        self._raise_anomaly(
            p["entry_id"],
            p["kind"],
            p["detail"],
            tuple(p.get("evidence_ids", ())),
            parse_ts(event["occurred_at"]),
            p.get("segment_index"),
            anomaly_id=p["anomaly_id"],
            emit=False,
        )
        return []

    def _raise_drafts(self, entry_id: str, drafts, raised_at) -> None:
        for draft in drafts:
            self._raise_anomaly(
                entry_id,
                draft.kind,
                draft.detail,
                draft.evidence_ids,
                raised_at,
                draft.segment_index,
            )

    def _raise_anomaly(
        self,
        entry_id: str,
        kind: str,
        detail: str,
        evidence_ids,
        raised_at,
        segment_index,
        anomaly_id: str | None = None,
        emit: bool = True,
    ) -> Anomaly:
        """登记异常；自动发现的异常同时以 ANOMALY_FLAGGED 事件留痕。"""
        if anomaly_id is None:
            self._anomaly_seq += 1
            anomaly_id = f"ANX-{self._anomaly_seq}"
        anomaly = Anomaly(
            anomaly_id,
            entry_id,
            kind,
            detail,
            tuple(evidence_ids),
            raised_at,
            segment_index,
        )
        self.anomalies[anomaly_id] = anomaly
        if emit:
            self.event_log.append(
                {
                    "event_id": f"evt-{anomaly_id}",
                    "event_type": "ANOMALY_FLAGGED",
                    "aggregate_type": "competition_entry",
                    "aggregate_id": entry_id,
                    "occurred_at": raised_at.isoformat(),
                    "version": 1,
                    "summary": f"自动标记异常 {kind}：{detail}",
                    "payload": {
                        "anomaly_id": anomaly_id,
                        "entry_id": entry_id,
                        "kind": kind,
                        "detail": detail,
                        "evidence_ids": list(evidence_ids),
                        "auto": True,
                    },
                }
            )
        return anomaly

    def _auto_resolve_gaps(self, entry_id: str) -> None:
        """漏站异常在证据补齐（含改属归位）后自动解除，无需处罚流程。"""
        book = self._books[self.entries[entry_id].format_id]
        for anomaly in self.anomalies.values():
            if (
                anomaly.entry_id == entry_id
                and anomaly.kind == "sequence_gap"
                and anomaly.status == ANOMALY_PENDING
                and anomaly.segment_index is not None
                and book.split(entry_id, anomaly.segment_index) is not None
            ):
                anomaly.status = ANOMALY_RESOLVED
                anomaly.resolved_by = "auto:evidence"

    # ---- 裁判认定 ----

    def _ruling_signed(self, event: dict) -> list[str]:
        p = event.get("payload") or {}
        errors = _require(
            p, ("category", "referee_id", "referee_role", "entry_id", "outcome", "reason")
        )
        if errors:
            return errors
        category = p["category"]
        if category not in RULING_CATEGORIES:
            return [f"未知认定类别：{category}"]
        if p["referee_role"] not in RULING_AUTHORITY[category]:
            return [f"裁判角色 {p['referee_role']} 无权认定 {category}"]
        entry = self.entries.get(p["entry_id"])
        if entry is None:
            return [f"报名不存在：{p['entry_id']}"]
        outcome = p["outcome"]
        if outcome not in RULING_OUTCOMES:
            return [f"未知认定结果：{outcome}"]
        targets = []
        for anomaly_id in p.get("anomaly_ids", ()):
            anomaly = self.anomalies.get(anomaly_id)
            if anomaly is None:
                return [f"异常不存在：{anomaly_id}"]
            if anomaly.entry_id != entry.entry_id:
                return [f"异常 {anomaly_id} 不属于 {entry.entry_id}"]
            if anomaly.status != ANOMALY_PENDING:
                return [f"异常 {anomaly_id} 已处理"]
            targets.append(anomaly)
        ruling = Ruling(
            event["aggregate_id"],
            category,
            p["referee_id"],
            p["referee_role"],
            entry.entry_id,
            tuple(a.anomaly_id for a in targets),
            outcome,
            p["reason"],
            parse_ts(event["occurred_at"]),
            int(p.get("penalty_seconds", 0)),
            int(p.get("credit_seconds", 0)),
            p.get("reassign_to"),
        )
        if outcome == "penalty" and ruling.penalty_seconds <= 0:
            return ["罚时必须为正整数秒"]
        if outcome == "time_adjustment" and ruling.credit_seconds <= 0:
            return ["减免秒数必须为正整数"]
        if outcome == "reassign_evidence" and ruling.reassign_to not in self.entries:
            return [f"改属目标报名不存在：{ruling.reassign_to}"]
        self._apply_ruling(entry, ruling, targets)
        self.rulings[ruling.ruling_id] = ruling
        return []

    def _apply_ruling(self, entry: Entry, ruling: Ruling, targets: list[Anomaly]) -> None:
        book = self._books[entry.format_id]
        if ruling.outcome == "penalty":
            entry.penalties.append(
                AppliedPenalty(
                    ruling.ruling_id,
                    ruling.category,
                    ruling.reason,
                    ruling.penalty_seconds,
                    ruling.referee_role,
                    ruling.signed_at.isoformat(),
                )
            )
        elif ruling.outcome == "time_adjustment":
            entry.time_credit_seconds += ruling.credit_seconds
        elif ruling.outcome == "confirm":
            for anomaly in targets:
                for evidence_id in anomaly.evidence_ids:
                    ev = self.evidence.get(evidence_id)
                    if ev is not None and ev.status == EVIDENCE_QUARANTINED:
                        ev.status = EVIDENCE_ACCEPTED
                        book.force_accept(ev)
        elif ruling.outcome == "exclude_evidence":
            for anomaly in targets:
                for evidence_id in anomaly.evidence_ids:
                    ev = self.evidence.get(evidence_id)
                    if ev is None:
                        continue
                    ev.status = EVIDENCE_EXCLUDED
                    split = book.split(entry.entry_id, ev.segment_index)
                    if split is not None and evidence_id in split.evidence_ids:
                        split.evidence_ids.remove(evidence_id)
                        remaining = [
                            self.evidence[x].occurred_at
                            for x in split.evidence_ids
                            if x in self.evidence
                        ]
                        split.recorded_at = max(remaining) if remaining else None
        elif ruling.outcome == "reassign_evidence":
            target = self.entries[ruling.reassign_to]
            target_book = self._books[target.format_id]
            for anomaly in targets:
                for evidence_id in anomaly.evidence_ids:
                    ev = self.evidence.get(evidence_id)
                    if ev is None or ev.status != EVIDENCE_QUARANTINED:
                        continue
                    ev.status = EVIDENCE_REASSIGNED
                    derived = Evidence(
                        f"{evidence_id}#re:{target.entry_id}",
                        target.entry_id,
                        ev.lane,
                        ev.segment_index,
                        ev.source,
                        ev.occurred_at,
                        ev.reported_at,
                        ev.device_seq,
                        dict(ev.metrics),
                    )
                    self.evidence[derived.evidence_id] = derived
                    drafts = target_book.ingest(derived, target.lane)
                    self._raise_drafts(target.entry_id, drafts, ev.occurred_at)
            self._auto_resolve_gaps(target.entry_id)
        for anomaly in targets:
            anomaly.status = ANOMALY_RESOLVED
            anomaly.resolved_by = ruling.ruling_id
        self._auto_resolve_gaps(entry.entry_id)

    # ---- 医疗 ----

    def _medical_stopped(self, event: dict) -> list[str]:
        entry = self.entries.get(event["aggregate_id"])
        if entry is None:
            return [f"报名不存在：{event['aggregate_id']}"]
        if entry.medical_hold:
            return ["已处于医疗停止中"]
        p = event.get("payload") or {}
        case = MedicalCase(entry.entry_id, parse_ts(event["occurred_at"]), p.get("reason", ""))
        entry.medical_hold = True
        self.medical_cases[entry.entry_id] = case
        return []

    def _return_authorized(self, event: dict) -> list[str]:
        entry = self.entries.get(event["aggregate_id"])
        if entry is None:
            return [f"报名不存在：{event['aggregate_id']}"]
        case = self.medical_cases.get(entry.entry_id)
        if not entry.medical_hold or case is None or case.status != "active":
            return ["无进行中的医疗停止"]
        p = event.get("payload") or {}
        if not p.get("authorized_by"):
            return ["恢复参赛必须注明授权人"]
        returned_at = parse_ts(event["occurred_at"])
        case.status = "returned"
        case.returned_at = returned_at
        case.authorized_by = p["authorized_by"]
        entry.medical_hold = False
        entry.medical_intervals.append(
            {
                "stopped_at": case.stopped_at.isoformat(),
                "returned_at": returned_at.isoformat(),
                "reason": case.reason,
                "authorized_by": case.authorized_by,
            }
        )
        return []

    # ---- 成绩发布 ----

    def _result_republished(self, event: dict) -> list[str]:
        p = event.get("payload") or {}
        errors = _require(p, ("format_id", "status"))
        if errors:
            return errors
        fmt = self.formats.get(p["format_id"])
        if fmt is None:
            return [f"赛制不存在：{p['format_id']}"]
        if p["status"] not in RELEASE_STATUSES:
            return [f"未知发布状态：{p['status']}"]
        stream_id = event["aggregate_id"]
        stream = self._stream_releases(stream_id)
        if event["version"] != len(stream) + 1:
            return ["发布版本必须递增"]
        if stream and stream[-1].status == "official" and p["status"] != "official":
            return ["正式成绩发布后只能以新的正式版本改判"]
        release = self._build_release(
            stream_id, event["version"], p["status"], parse_ts(event["occurred_at"]), fmt
        )
        self.releases.append(release)
        return []

    def _stream_releases(self, stream_id: str) -> list[Release]:
        return [r for r in self.releases if r.release_id == stream_id]

    def _build_release(self, stream_id, version, status, published_at, fmt) -> Release:
        book = self._books[fmt.format_id]
        entries = sorted(
            (e for e in self.entries.values() if e.format_id == fmt.format_id),
            key=lambda e: e.entry_id,
        )
        results = [self._build_entry_result(e, fmt, book) for e in entries]
        ranked = sorted(
            (r for r in results if r.standing == STANDING_RANKED),
            key=lambda r: (r.total_seconds, r.entry_id),
        )
        for rank, result in enumerate(ranked, 1):
            result.rank = rank
        results.sort(
            key=lambda r: (r.rank is None, r.rank if r.rank is not None else 0, r.entry_id)
        )
        for result in results:
            result.digest = digest_of(result_signing_payload(result))
        stream = self._stream_releases(stream_id)
        rules = deepcopy(fmt.rules)
        rules["format_id"] = fmt.format_id
        rules["format_version"] = fmt.version
        release = Release(
            stream_id,
            version,
            status,
            published_at,
            rules,
            results,
            stream[-1].digest if stream else None,
        )
        release.digest = digest_of(release_signing_payload(release))
        return release

    def _build_entry_result(self, entry: Entry, fmt: RaceFormat, book: AssemblyBook) -> EntryResult:
        pending = [
            a
            for a in self.anomalies.values()
            if a.entry_id == entry.entry_id and a.status == ANOMALY_PENDING
        ]
        pending_segments = {a.segment_index for a in pending if a.segment_index is not None}
        wave_start = fmt.waves.get(entry.wave)
        splits: list[SplitView] = []
        prev = wave_start
        complete = True
        last_recorded = None
        for seg in fmt.segments:
            split = book.split(entry.entry_id, seg.index)
            quarantined = any(
                ev.entry_id == entry.entry_id
                and ev.segment_index == seg.index
                and ev.status == EVIDENCE_QUARANTINED
                for ev in self.evidence.values()
            )
            disputed = seg.index in pending_segments or quarantined
            if split is not None and split.recorded_at is not None:
                duration = (
                    round((split.recorded_at - prev).total_seconds(), 3)
                    if prev is not None
                    else None
                )
                prev = split.recorded_at
                last_recorded = split.recorded_at
                sources = [
                    self.evidence[eid].source
                    for eid in split.evidence_ids
                    if eid in self.evidence
                ]
                splits.append(
                    SplitView(
                        seg.index,
                        seg.kind,
                        seg.station_id,
                        duration,
                        split.recorded_at.isoformat(),
                        list(split.evidence_ids),
                        sources,
                        disputed,
                        list(split.notices),
                    )
                )
            else:
                complete = False
                prev = None  # 缺环节后不再推算跨段时长
                splits.append(
                    SplitView(seg.index, seg.kind, seg.station_id, None, None, [], [], disputed, [])
                )
        # 医疗停止高于计时流程；待审不处罚，但也不给正式名次。
        if entry.medical_hold:
            standing = STANDING_MEDICAL
        elif pending:
            standing = STANDING_PENDING
        elif not complete:
            standing = STANDING_INCOMPLETE
        else:
            standing = STANDING_RANKED
        total = None
        if complete and wave_start is not None and last_recorded is not None:
            total = round(
                (last_recorded - wave_start).total_seconds()
                + sum(pen.seconds for pen in entry.penalties)
                - entry.time_credit_seconds,
                3,
            )
        penalties = [
            PenaltyView(pen.ruling_id, pen.category, pen.reason, pen.seconds, pen.referee_role)
            for pen in entry.penalties
        ]
        freeze = FreezeSnapshot(
            fmt.format_id,
            fmt.version,
            entry.group,
            entry.wave,
            entry.version,
            entry.athlete_ids,
            deepcopy(entry.partner_eligibility),
            {a.apparatus_id: a.status for a in self.apparatus.values() if a.lane == entry.lane},
        )
        return EntryResult(
            entry.entry_id,
            standing,
            None,
            total,
            sum(pen.seconds for pen in penalties),
            entry.time_credit_seconds,
            penalties,
            splits,
            freeze,
        )

    def release(self, stream_id: str, version: int) -> Release:
        for rel in self._stream_releases(stream_id):
            if rel.version == version:
                return rel
        raise KeyError(f"发布不存在：{stream_id} v{version}")

    # ---- 证书与奖励递补 ----

    def issue_certificates(self, stream_id: str, version: int) -> list[Certificate]:
        """按正式版本签发证书；新版本签发后旧证书自动作废。同版本重复调用幂等。"""
        release = self.release(stream_id, version)
        if release.status != "official":
            raise ValueError("仅正式成绩可签发证书")
        existing = [
            c
            for c in self.certificates.values()
            if c.release_id == stream_id and c.release_version == version
        ]
        if existing:
            return sorted(existing, key=lambda c: c.rank)
        issued = []
        for result in release.results:
            if result.standing != STANDING_RANKED:
                continue
            certificate_id = f"{stream_id}-v{version}-{result.entry_id}"
            cert = Certificate(
                certificate_id,
                stream_id,
                version,
                result.entry_id,
                result.rank,
                result.freeze.group,
                result.total_seconds,
                result.digest,
            )
            for old in self.certificates.values():
                if (
                    old.release_id == stream_id
                    and old.entry_id == result.entry_id
                    and old.status == "valid"
                ):
                    old.status = "superseded"
                    old.superseded_by = certificate_id
            self.certificates[certificate_id] = cert
            issued.append(cert)
        return sorted(issued, key=lambda c: c.rank)

    def award_report(self, stream_id: str, version: int) -> dict:
        """按正式版本计算各组获奖名次，并与上一正式版本比对出递补与撤销。"""
        release = self.release(stream_id, version)
        if release.status != "official":
            raise ValueError("奖励递补仅依据正式成绩")
        places = int(release.rules.get("award_places", 3))
        current = self._awards_of(release, places)
        previous = None
        for rel in reversed(self._stream_releases(stream_id)):
            if rel.version < version and rel.status == "official":
                previous = rel
                break
        prior = (
            self._awards_of(previous, int(previous.rules.get("award_places", 3)))
            if previous
            else {}
        )
        changes = {}
        for group in sorted(set(current) | set(prior)):
            current_ids = [a["entry_id"] for a in current.get(group, [])]
            prior_ids = [a["entry_id"] for a in prior.get(group, [])]
            changes[group] = {
                "backfill": [e for e in current_ids if e not in prior_ids],
                "revoked": [e for e in prior_ids if e not in current_ids],
            }
        return {
            "release_id": stream_id,
            "version": version,
            "compared_to": previous.version if previous else None,
            "awards": current,
            "changes": changes,
        }

    @staticmethod
    def _awards_of(release: Release, places: int) -> dict:
        groups: dict[str, list] = {}
        for result in release.results:
            if result.standing == STANDING_RANKED:
                groups.setdefault(result.freeze.group, []).append(result)
        awards = {}
        for group, results in groups.items():
            results.sort(key=lambda r: r.rank)
            awards[group] = [
                {
                    "place": i + 1,
                    "entry_id": r.entry_id,
                    "rank": r.rank,
                    "total_seconds": r.total_seconds,
                }
                for i, r in enumerate(results[:places])
            ]
        return awards

    # ---- 对外视图与验证 ----

    def entry_statement(self, entry_id: str, stream_id: str | None = None, version: int | None = None) -> dict:
        """参赛者视图：分段、用时、处罚理由与冻结上下文。"""
        found = None
        for rel in self.releases:
            if stream_id and rel.release_id != stream_id:
                continue
            if version is not None and rel.version != version:
                continue
            for result in rel.results:
                if result.entry_id == entry_id:
                    found = (rel, result)
        if found is None:
            raise KeyError(f"无成绩发布记录：{entry_id}")
        rel, result = found
        entry = self.entries[entry_id]
        return {
            "entry_id": entry_id,
            "release": {
                "release_id": rel.release_id,
                "version": rel.version,
                "status": rel.status,
                "digest": rel.digest,
            },
            "standing": result.standing,
            "rank": result.rank,
            "total_seconds": result.total_seconds,
            "penalties": [penalty_payload(p) for p in result.penalties],
            "splits": [split_view_payload(s) for s in result.splits],
            "freeze": freeze_payload(result.freeze),
            "medical_intervals": [dict(m) for m in entry.medical_intervals],
            "under_medical_hold": entry.medical_hold,
        }

    def conflict_report(self, entry_id: str | None = None) -> list[dict]:
        """裁判视图：异常及其证据来源（赛道、来源、时间），定位冲突。"""
        rows = []
        for anomaly in self.anomalies.values():
            if entry_id and anomaly.entry_id != entry_id:
                continue
            evidence = []
            for evidence_id in anomaly.evidence_ids:
                ev = self.evidence.get(evidence_id)
                if ev is None:
                    continue
                holder = self.entries.get(ev.entry_id)
                evidence.append(
                    {
                        "evidence_id": ev.evidence_id,
                        "source": ev.source,
                        "lane": ev.lane,
                        "entry_lane": holder.lane if holder else None,
                        "occurred_at": ev.occurred_at.isoformat(),
                        "status": ev.status,
                    }
                )
            rows.append(
                {
                    "anomaly_id": anomaly.anomaly_id,
                    "entry_id": anomaly.entry_id,
                    "kind": anomaly.kind,
                    "status": anomaly.status,
                    "detail": anomaly.detail,
                    "segment_index": anomaly.segment_index,
                    "raised_at": anomaly.raised_at.isoformat(),
                    "resolved_by": anomaly.resolved_by,
                    "evidence": evidence,
                }
            )
        return rows

    def public_release(self, stream_id: str, version: int) -> dict:
        """公开视图：名次、各成绩摘要与版本链，供任何人验证。"""
        rel = self.release(stream_id, version)
        return {
            "release_id": rel.release_id,
            "version": rel.version,
            "status": rel.status,
            "published_at": rel.published_at.isoformat(),
            "digest": rel.digest,
            "previous_digest": rel.previous_digest,
            "ranked": [
                {
                    "entry_id": r.entry_id,
                    "rank": r.rank,
                    "total_seconds": r.total_seconds,
                    "result_digest": r.digest,
                }
                for r in rel.results
                if r.standing == STANDING_RANKED
            ],
            "standings": {r.entry_id: r.standing for r in rel.results},
        }

    def export_release(self, stream_id: str, version: int) -> dict:
        """导出完整发布载荷（含摘要），供外部独立验证。"""
        return release_payload(self.release(stream_id, version))

    def verify_chain(self, stream_id: str) -> list[str]:
        """重算发布流每个版本的摘要并校验前序链接；返回问题列表（空为通过）。"""
        errors = []
        prev = None
        for rel in self._stream_releases(stream_id):
            expected = prev.digest if prev else None
            if rel.previous_digest != expected:
                errors.append(f"{stream_id} v{rel.version} 前序摘要断裂")
            errors.extend(verify_release_payload(release_payload(rel)))
            prev = rel
        return errors
