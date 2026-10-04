# 混合体能完赛裁决后端

为万人混合体能赛（16 个跑段/固定项目交替环节）提供事件溯源的完赛裁决后端：
计时芯片、项目裁判、器械传感器的记录按**真实发生时间**组成跑段与固定项目序列；
迟到或重复上报不多算环节；校验异常**只进入待审，不直接处罚**；医疗停止高于
计时流程；漏站、设备故障、志愿者误导、运动员违规由不同权责的裁判分别认定；
初榜、申诉中、正式成绩逐版本保留规则与证据，改判后以新版本完成名次、证书与
奖励递补。

## 目录

- `contracts/domain.schema.json`：领域事件信封（必填七字段）与稳定枚举。
- `data/sample.json`：中文联调样例（一条 ENTRY_CONFIRMED 事件）。
- `src/contracts.py`：事件/聚合/异常/认定/发布阶段枚举与裁判权责表。
- `src/validator.py`：信封校验（保留原 `validate_event`，新增严格 `validate_envelope`）。
- `src/store.py`：仅追加事件存储、`event_id` 幂等、聚合版本、哈希链与 JSONL 日志。
- `src/projections.py`：从事件流重建赛制、报名、器械、分枪、证据、医疗、待审、
  认定、申诉、发布等投影。
- `src/app.py`：`AdjudicationService` 裁决服务（全部用例）。
- `tests/`：信封契约、存储幂等/哈希链单测与 10 个端到端场景测试类
  （错道串号、双人替换、迟到重排、医疗停表、四类认定权责、申诉改判递补、
  设备故障窗口、搭档资格冻结、日志重放等，共 19 个测试）。

## 核心规则

### 信封（所有接入事件必须遵守）

`event_id / event_type / aggregate_type / aggregate_id / occurred_at / version /
summary` 七个必填字段；`occurred_at` 必须带时区偏移；业务数据放 `payload`。
事件类型与聚合类型只能取自 `domain.schema.json` 枚举，新增只许追加。

### 证据接入与 16 环节组装

- 三种来源：`timing_chip`（计时门）、`station_judge`（项目裁判完成确认）、
  `apparatus_sensor`（器械传感器）。
- 归位依据 `observed_at`（真实发生时间）；`received_at` 只用于判定迟到。
- 同位置重复读数 → `EVIDENCE_REJECTED` + 重复待审，环节不增加；同一
  `event_id` 重放直接返回已存事件（迟到重传不多算）。
- 组装要求计时门连续；某门缺失时只组装到缺口为止，门证据迟到补齐后续装，
  已组装环节携带 `supersedes` 链接重排而不是新增。
- 双人组每个计时门以两人均过门的较晚时刻为准。
- 固定项目须在对应门窗口内有裁判确认；要求传感器的项目还须有传感器读数。
- 相邻赛道器械串号、bib/芯片与报名不符、器械非 NOMINAL 期间读数、物理不可能
  速度、时间倒置等 → 只立 `ANOMALY_FLAGGED` 待审案件，不产生任何处罚。

### 赛制、报名、器械冻结

- 赛制登记时固定 16 环节（run/station 严格交替）与规则版本。
- 双人组赛前最多替换一次搭档：须在发枪前、无开赛证据、新搭档资格通过且未在
  同枪其他有效报名中。
- 器械登记绑定赛道；故障/恢复按真实时间成线，故障窗口读数只进待审。
- 每份成绩发布都冻结：赛制（含环节摘要）、分枪真实发枪时间、报名（组别/赛道/
  搭档资格与替换记录）、器械状态时间线摘要、证据清单，以及快照 sha256。

### 医疗

- `MEDICAL_STOP_ISSUED` 后停止期间的一切证据拒收（高于计时流程）。
- `MEDICAL_RESUME_AUTHORIZED` 必须由停止发起人之外的授权人批准。
- 经授权的医疗停表时间从总时长中扣除。

### 裁判认定与申诉

| 认定类型 | 有权角色 | 允许的处理 |
| --- | --- | ---|
| 漏站 missed_station | station_referee | 加时 / 不罚 / DQ |
| 设备故障 equipment_fault | technical_delegate | 修正计时 / 不罚（禁止处罚） |
| 志愿者误导 volunteer_misdirection | chief_course_judge | 修正计时 / 不罚（禁止处罚） |
| 运动员违规 athlete_violation | competition_jury | 加时 / DQ / 不罚 |

处罚只能来自签署的 `RULING_SIGNED`；待审异常本身绝不计罚。申诉须在认定后
规定时限内提出；申诉成立时新认定必须显式 `supersedes_ruling_id` 取代旧认定，
旧处罚立即失效。

### 成绩发布与三类视图

- 阶段：`preliminary`（初榜）→ `under_appeal`（申诉中状态）→ `official`
  （正式锁定）。存在未决申诉时只可发布申诉中状态；存在未决待审/申诉时禁止
  锁定正式成绩。
- 改判后以新版本重排名次；前三名奖励持有人变化时发出带
  `previous_entry_id` 的 `AWARD_REROLLED`（递补）；证书逐版本重发并链接
  被取代证书。
- 参赛者视图 `participant_view`：逐段时间、医疗扣除、加时/修正、**处罚理由**
  与历次发布名次。
- 裁判视图 `referee_view`：每个待审案件的异常明细、关联读数来源与认定结果，
  用于定位冲突来源。
- 公开视图 `public_results` / `verify_latest_release`：名次、冻结摘要、
  快照哈希与事件哈希链；任何事件被篡改都会使 `chain_intact` 失败。

## 本地检查

```bash
python3 -m unittest discover -s tests -v
```
