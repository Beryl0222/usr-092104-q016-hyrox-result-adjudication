# 混合体能完赛裁决

本仓库保存混合体能完赛裁决的领域词汇、事件约定与裁决后端代码，供相关单位统一对象身份、事件顺序和版本语义。

## 目录

- `contracts/domain.schema.json`：领域事件信封与稳定枚举。
- `data/sample.json`：一条中文联调样例（信封最小样例）。
- `data/sample_events.json`：完整联调事件流（串道改属、赛前换搭档、医疗停止、初榜 → 申诉中 → 正式 → 改判递补）。
- `scripts/gen_sample_events.py`：生成上述事件流，供联调复现。
- `src/validator.py`：事件信封基础字段校验。
- `src/envelope.py`：事件类型、聚合类型与事件-聚合配对约束。
- `src/model.py`：领域对象与公开摘要（sha256）工具。
- `src/assembly.py`：多来源记录按真实发生时间合成跑段与固定项目序列。
- `src/engine.py`：裁决引擎（证据、异常、认定、医疗、成绩版本、证书与奖励递补）。
- `tests/`：领域资料一致性与裁决行为检查。

## 核心对象

race_format（赛制）、competition_entry（报名）、split_evidence（分段证据）、result_release（成绩发布）、apparatus（器械状态）、ruling（裁判认定）。

## 已登记事件

ENTRY_CONFIRMED、SPLIT_RECORDED、ANOMALY_FLAGGED、RULING_SIGNED、RESULT_REPUBLISHED、FORMAT_PUBLISHED、APPARATUS_STATUS_CHANGED、MEDICAL_STOPPED、RETURN_AUTHORIZED。

后续服务应保持事件兼容：只允许扩展枚举取值，不得修改或删除既有取值。

## 裁决原则

- 计时芯片、项目裁判与器械传感器记录一律按信封 `occurred_at`（真实发生时间）归并入（报名者, 环节）；迟到与重复上报只追加提示，不多算环节。
- 车道不符、环节缺失、时间倒挂、器械异常等校验异常只进入待审（pending_review），不直接产生处罚。
- 漏站、设备故障、志愿者误导、运动员违规分别由有权裁判认定（角色权限见 `src/model.py` 的 `RULING_AUTHORITY`）；越权认定直接拒绝，不改变任何状态。
- 医疗停止高于计时流程：停止期间不参与排名；恢复参赛必须凭 RETURN_AUTHORIZED 授权。
- 每份成绩冻结赛制版本、组别、分枪、搭档资格与器械状态；初榜、申诉中、正式成绩都保留对应规则与证据引用。
- 改判只以新的发布版本完成，名次、证书与奖励递补随新版本重算；历史版本不可改写。
- 公开结果携带 sha256 摘要与前序摘要链，任何一方可重算验证（`export_release` / `verify_release_payload` / `verify_chain`）。

## 本地检查

```bash
python3 -m unittest discover -s tests
```
