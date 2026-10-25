# FileButler 路由与任务协议详细修改方案

日期：2026-10-24（Asia/Taipei）  
作者角色：资深工程师 Agent  
状态：方案设计已通过独立产品复审；未实施生产代码。

实施状态（2026-10-25）：S0–S1（`1a20c5f`）、S2（`cb070d1`）、S5（`aa27657`）、S4（`9e5571e`）、S3（`c2ec16f`）已实施，离线 476 passed、无 xfail。`Selection.part` 从 S4 提前到 S1。容量：去掉 pydantic 自动生成的 schema title（3,661 字，无规则内容，不在第 10 节所列删减项内，单独说明），并删去第 10 节所列的重复说明；最终提示 31,038 / 31,142（上限 31,311）。第 40 轮 schema title A/B（各 3 遍）：去掉 title 90.2%、保留 96.0%，差异不显著，但嵌套提问写错是报错主因；按上文修正平铺后提示 30,830 / 30,934；第 41 轮复验 31/34、失败工具调用 31→5。之后按 `docs/s5-routing-audit-2026-10-25.md` 修复 P1-A/P1-B/P2-A/P2-C 与"不做"的业务契约（`report.result=by_choice`，库版本 4），提示 31,012 / 31,116。之后第 42–44 轮修复带作用域的选择结算（`53137ce`），回归 32/34。2026-10-25 S6–S8 已实施（`docs/s6-s8-implementation-2026-10-25.md`）：S6a 三维状态与判定器 v4 并列（重判 778 个场景：v3 675、v4 680，5 处差异均为第 7 节有意的改变）；S6b 结构化工具结果与 16 类错误；S7 检索覆盖与账本（7 个冻结样例 7/7；未加批量查询和“找全”字段，因容量）；S8 真实 Chrome 验收 6/6、真实库副本迁移与回滚演练。离线 526 passed，提示 31,048 / 31,152。第 45 轮真实模型验收（5 套件×3 遍，¥1.29）v3 61/72、v4 63/72，修复已答问题残留与工具结果序列化（`41e28a8`，529 passed）；第 46 轮复验修复生效（¥0.34）。与本方案逐节对照的未完成与偏差见 `docs/s6-s8-implementation-2026-10-25.md` 第 9 节。

审核记录：[独立产品审核报告](agent-routing-redesign-2026-10-24.product-review.md)。初稿与修订稿保留供审计。

## 1. 决策摘要与修订后的发布切片

建议保留单 Agent 与协议 v3 主体：模型解释需求，本地管理身份、权限、执行事实与核验。此次改造优先封住“只做 a 却证明 a+b 已完成”的当前 P0；问题身份、消息幂等、局部作用域分开发布，强终止协议另设启用闸门。不新增固定意图分类请求，不默认动态换 schema。

本次产品初审退回的主要原因已纳入正文：幂等读取顺序、所有消息的接收事实、取消/暂停/跳过的区别、实施依赖、schema 容量、确认结果来源。修订版不再把整个交互重构塞入一个首批交付。

| 切片 | 可发布交付 | 前置与范围 | 预计工程量 |
| --- | --- | --- | --- |
| S0 | 冻结复现、完整基线、schema 原型、迁移样本 | 已有基线未全绿；实施前隔离 OCR | 1–2 人日 |
| S1 | 封住 P0 多认领；保留确认 approved/declined/timed_out/interrupted 来源；本轮拒绝同清单不重弹 | 仅已有 Action.part 与本地字段；新旧模糊计划恢复安全；首个发布物 | 2–3 人日 |
| S2 | 所有消息统一接收事实/幂等/outbox，精确问题 ID 与按钮回答 | 依赖 S1；模型 schema 不增字段；局部作用域未知时继续保守阻塞，不宣称已修复局部选择 | 4–6 人日 |
| S3 | 草案目标、局部问题/决定、自由文本输入关系、取消/暂停/恢复 | 依赖 S1+S2；扩展模型字段须先过容量闸门；否则只集成不启用 | 4–6 人日 |
| S4 | 同类去重显式 part、只读 evidence claim、选择集精确归属 | 依赖 S1，消息跨轮功能依赖 S2；字段容量闸门独立 | 2–4 人日 |
| S5 | finish_turn 与严格终止切换 | 独立候选，依赖相关协议原型/纯问答测试；不是 S2 前置 | 2–4 人日 |
| S6a/S6b | 状态维度拆分、结构化工具结果/恢复，逐工具迁移 | S1–S4 稳定后；保留旧 UI 产品语义与旧评测 | 4–7 人日 |
| S7 | 检索覆盖版本账本、有界语言扩展 | 可延后，不承诺召回提升比例 | 2–4 人日 |
| S8 | 各批验证之外的收尾联调、迁移/回滚与浏览器验收 | 按切片组合执行 | 2–3 人日 |

全部约 **23–39 人日**，只是单人串行开发/验证估算，不是日期承诺。当前建议批准的可实施最小范围为 **S0–S2，约 7–11 人日**：其中 S1 可独立先发布，S2 不等待强终止或草案 schema。S3–S5 的产品契约在本文明确，但新增模型字段没有容纳证据前不算已获实施启用批准；S6–S7 属后续排期。OCR 根因、供应商兼容和迁移数据复杂度可能增加工期。

依赖图：`S0 → S1 → S2 → S3`；`S1 → S4（跨轮部分再依赖S2）`；`S5` 独立过容量/终止回归闸门后接入；`S3/S4 → S6 → S7` 为建议集成顺序。不能先启用局部决定再等后片补精确动作归属。

必须保留：具体权限卡、可撤销、原子不覆盖、隐藏文件分级、敏感检测默认关闭且开启后读取需明确点选授权、单计划最多 500 项且不自动分批、用户限制不可暗放宽、费用按运行与读取按任务累计、缓存策略、31,311 字符硬上限。问题/范围估计/证据归属不产生授权。向量检索、通用非空目录回收、持久索引、SSE、完整通用执行器与跨轮语义等价拒绝指纹均为独立后续工作。

方案复审通过代表契约可实施，不代表代码完成、当前测试全绿或所有自然语言都正确。

## 2. 本次核实的现状与证据等级

读取了 README、HANDOFF 第 1/7/8 节、2026-10-23 链路方案，以及主循环、控制器、提问、收尾、意图编译、策略、归属、任务存储、核验、计划执行、注册层、检索、去重、Web 消息/选项与相关测试。工作区已有 README、HANDOFF、engineering-notes、feature-plan 等未提交修改，本次不编辑它们。

标记含义：**已复现**是本次用当前代码/测试验证；**历史证据**来自已有日志/文档，未重跑模型；**代码确认**是当前实现可直接读出；**待验证推断**不能当作线上事故。

| 发现 | 等级与位置 | 结论 |
| --- | --- | --- |
| 显式 `goal_ids=[g1,g2]`，动作均不写 `part`，只归档 a 仍让“改名 b”通过 | 本次已复现；`file_plans._attribute_steps`、`verifier._goal_steps` | P0：目标错误完成；b.txt 实际未改名，两目标均 done |
| 针对 b 的空动作选项阻止 a 的计划；重发 A 被下一问消费 | 当前 `test_answer_replays.py` 两项 strict xfail 已复现 | P0：选项范围与回答关联不完整；不能称为已在线上观察到的两类事故 |
| 两个 dedup 部分无法显式选择当前目标 | 当前 `test_protocol_v3.py::test_two_parts_verifying_receipts_still_need_an_explicit_step` 通过，证明当前主动拒绝；`active_goals` | 正常复合需求受阻；工具 API 无目标参数，不能要求用户改业务目标来适配执行器 |
| 正文出现问题/选项但不调用 ask_user | HANDOFF 第 8 节用户日志两次，`finish_plain` 代码确认 | 历史实际案例，尚缺本次冻结回放；不能靠关键词作为正式路由 |
| 只读任务登记后内部 needs_review，直接回答 answered | TEMPLATES、task_state、display_state 测试确认；第 37/39 轮历史 | 网页已显示“已回答”；仍存在内部状态/评测语义不一致，不再声称 UI 尚未修复 |
| file_info 说图片不能 read_file；read_file 实际已 OCR | 当前工具说明冲突，代码确认 | 可直接精简修复，但说明收益需测量 |
| ToolResult 只有 status/text/error_code/retryable；业务依赖 artifact 增量与 runtime 副作用 | registry、task_runtime.after、findings | 结构风险；目前不是所有工具都故障 |
| BM25 跨语言弱；OCR 有 20 秒上限，覆盖统计在文字里 | 当前 retrieval 与冻结 61 题历史 | 补机器可读覆盖和有限策略切换；不把“无词项”当“无相关内容” |
| 旧快照累积、点选后不恢复原任务、无 goal_ids 默认多认领 | 2026-10-23 已修，有对应测试 | 本次不重复重做；只补剩余缺口 |

本次工程定向命令：

```sh
.venv/bin/python -m pytest -q tests/test_answer_replays.py tests/test_display_state.py tests/test_history_snapshot.py tests/test_protocol_v3.py -k 'answering_bs or resent_letter or two_parts_verifying_receipts or pre_task_evidence or named_part or one_plan_proves or display or snapshot or untagged_actions'
```

结果：9 passed、2 xfailed、111 deselected、5 warnings。另在 `TemporaryDirectory` 中通过现有测试夹具准备并执行上述双目标单动作计划，确认两个目标均 passed 而 b.txt 仍存在；只使用临时文件和本地工具，没有调用模型。

主线程本次完整离线结果为 **431 passed、4 failed、2 xfailed、7 warnings**。4 项是 `test_ocr.py` 的图片、扫描 PDF、旋转照片识别，以及 `test_week1.py::test_read_tools`；见到 `Foundation._GenericObjCError error 0` 或空识别结果。只能确认当前执行环境 OCR 调用失败，根因尚未隔离，不能断言只是沙盒，也不能定性为路由回归。S0 需要建立可复现环境与平台失败分类；不得靠 xfail、删除断言或忽略失败把基线刷绿。

### 2.1 当前 P0 的完整离线复现

在项目根目录运行下列命令。脚本只在 `TemporaryDirectory` 生成合成文件，`Confirmer("never")` 仅为这些临时文件避免交互，不修改产品确认策略；不读取 sandbox/data/real，不调用模型/API。导入的 Session 夹具均来自仓库 tests。

```sh
.venv/bin/python - <<'PYCODE'
import sys, tempfile
from pathlib import Path
sys.path.insert(0, "tests")
from test_protocol_v3 import two_part_runtime, ARCHIVE_A, part_checks
from agent.file_plans import prepare, PrepareFilePlan, execute
from safety.oplog import OpLog
from safety.confirm import Confirmer

with tempfile.TemporaryDirectory(prefix="filebutler-routing-audit-") as directory:
    root = Path(directory)
    runtime, sandbox = two_part_runtime(root)
    # g1: 归档 a；g2: 把 b 改名 c；本计划实际上只有 move a。
    plan = prepare(runtime, PrepareFilePlan(
        description="只归档 a，却声明两个目标",
        actions=ARCHIVE_A, goal_ids=["g1", "g2"]))
    execute(runtime, plan["id"], OpLog(sandbox, "audit"), Confirmer("never"))
    checked = part_checks(runtime, sandbox)
    print({"step_owners": [step["goal_ids"] for step in plan["payload"]["steps"]],
           "checks": {gid: row["passed"] for gid, row in checked.items()},
           "b_still_exists": (root / "Inbox/b.txt").exists(),
           "c_exists": (root / "Inbox/c.txt").exists()})
PYCODE
```

本次实际：`step_owners=[["g1","g2"]]`、`checks={"g1":True,"g2":True}`、`b_still_exists=True`、`c_exists=False`。
预期改造后：准备阶段返回 `AMBIGUOUS_GOAL`，未执行任何文件操作；显式补 `part="g1"` 并仅绑定 g1 后，g1 通过、g2 未完成。此用例是首批发布阻断项。

## 3. 路由分层与不变量

路由分成三层，避免一个“intent”标签控制所有事情：

1. **输入关系**：当前消息是精确回答、补充/修订、续做、新请求还是独立查询。结构化按钮本地确定；自由文字由同一次常规模型调用解释。
2. **流程动作**：读取、检索、提问、准备、执行、收尾。模型选择，控制器验证当前状态是否允许。
3. **证明关系**：哪个目标被哪个动作、回执、证据或用户跳过决定满足。由本地结构检查与事实核验决定。

不变量：

- root/session/request/task/revision 是引用边界；跨会话或旧版本引用不生效。
- 只读请求可以不建 Task，仍有轻量 Request；Request 无写权限、不进入任务列表、不增加常规产品交互。
- 每个非依赖写动作必须有一个确定的主要目标；辅助建目录可以共享依赖，但不能代替其他目标的业务动作。
- 用户跳过只结算明确的目标或明确的目标内子范围。未知范围只能等待澄清，不能“其余目标全部排除”。
- 问题回答记录不等于执行确认；授权变化仍按原规则展示具体卡片。
- 请求、任务修订、恢复、重新建计划都不能重置既有任务读/检索计数、费用账本或已执行事实。费用目前按运行预算、读/检索按任务累计，保持已有语义，不把费用预算擅自改成终生任务限额。
- 模型上下文只投影必要字段，完整协议/证据放本地持久层。

## 4. 统一提问与终止协议

### 4.1 目标 API（S5，独立容量闸门后启用）

后续候选引入一个稳定的终止工具 `finish_turn`，替换模型主路径中的 `ask_user` 与 `finish_task`，同时覆盖普通回复。不是新意图分类调用；模型在本来应结束的同一次响应调用工具，本地立即生成报告并停止，不再让模型为确认“结束成功”追加一次请求。

```python
FinishTurn = {
  "mode": "reply | ask | task",
  "reply": "用户主回复，可空",
  "question": "问题文字", "reason": ..., "options": [...],   # 仅 ask；AskPayload 的字段平铺在这一层（2026-10-25 修正，见下）
  "summary": "模型补充，可空",         # task；沿用输出长度与detail语义
  "unfinished": [{"part_id": "g2", "reason": "..."}],
  "not_needed": [{"part_id": "g3", "reason": "..."}],
  "input_resolution": InputResolution | None
}
```

用枚举与本地 validator 保证字段组合互斥；如果模型供应商对 JSON Schema union 支持不稳，不必依赖复杂 discriminated union。

2026-10-25 实施修正：最初按 `question: AskPayload` 嵌套实现，第 40 轮真实运行中模型 11 次把 reason/subject/scope 写到外层或编出 question_reason（`question` 既是对象名又是 AskPayload 里的问题文字）。现改为 AskPayload 的字段直接平铺在 finish_turn 上（question 为问题文字），mode=ask 必填 question/reason/options，其他 mode 不得带提问字段；旧嵌套写法由本地展开兼容，不出现在 schema 中。提示字数因此 −208。`reply` 不创建 Task；已有未结任务时不能用 reply 绕过本地核验。`task` 必须有当前 Task；`ask` 必须先持久化问题再结束本轮。

旧 `ask_user`/`finish_task` 保留内部兼容 adapter 和 trace 解析，不同时长期暴露新旧三份大 schema。切换以会话协议版本/服务配置确定，单个会话在一轮内 schema 稳定。S2 继续使用现有 ask_user/finish_task 与普通正文路径；只统一内部终止事件和问题引用，不增加模型参数。S5 才引入内部 `TurnEnd` 联合类型并原子切换模型 schema。S5 未通过时，正文提问的历史缺口必须公开保留，不能声称已解决。

普通模型正文只作为过程说明；严格终止协议启用后，无工具直接结束属于 `TERMINAL_REQUIRED`，允许至多一次协议纠正（计入原预算、repeat/no-progress 保护），仍失败则保留草稿并报告“回复未形成完整交互，请重试”，不宣称 answered、不得执行后续写入。兼容模式下纯正文仍可保留历史行为，但打 `unstructured_terminal` 诊断，不猜它是不是问题；严格路径必须覆盖纯聊天回归后才能默认启用。

正式判断不扫描“是否”“要不要”“A/B”等词。正文提问错误靠强制显式终止结构与回放发现。该协议只能保证输出类型明确，不能保证模型不会把一个问题错误标为 reply；后者仍需语义评测及产品审查。

同一模型响应包含多工具时仍顺序处理；终止入口一旦成功，后续调用返回 `TURN_CLOSED` 且不执行，所有 tool_call/result 保持成对。持久化失败不展示可点击问题；已经发生的写入仍按操作日志报告，不声称本轮没有动作。

### 4.2 Request 与登记前问题（S2 最小 Request；S3 才启用草案映射）

新增轻量 Request 记录，承载原始用户请求和登记前的局部作用域：

```json
{
  "request_id": "U...", "root": "服务端资料库身份", "session": "会话ID",
  "source_message_id": "消息ID", "parent_request_id": null,
  "status": "active", "draft_revision": 1,
  "draft_parts": [{"id": "d_b", "description": "移动 b.txt", "subjects": ["Inbox/b.txt"]}],
  "task_id": null
}
```

Request 不等于任务登记，不携带 allowed_effects/write_roots。纯解释可以只保存最小记录，草案目标仅提问需要局部范围时建立。`draft_parts` 是模型声明并接受本地 ID/路径校验的数据，不声称已从自然语言证明“这就是全部目标”。

AskPayload 增加短字段：`scope={level: request|parts, part_ids:[...]}`。task 存在时 part_ids 指当前目标；不存在时指 draft_parts（可在该次提问携带最小草案局部项，由本地生成/校验 ID）。全局选择必须明确 `level=request`，不把空 actions 自动解释为全局取消。`subject` 继续代表涉及对象，不能替代目标范围。

后续 `start_task` 在 Part 上支持可选 `draft_id`（仅登记前问题映射所需）。模型明确给出草案到正式目标映射；控制器检查 draft 与 Task 同属 Request、ID 唯一、问题影响项均已映射。不能用路径相似度或共同父目录猜映射。没有映射时问题决定可以保存，但不能据此跳过目标、放行覆盖或敏感读取；返回 `UNRESOLVED_QUESTION_SCOPE` 要求补全，写权限卡继续兜底。将一个已作决定的草案部分并入混合目标时，不允许跳过整个混合目标；必须保留独立目标，或表达子范围决定。

### 4.3 持久化问题与选项（S2 身份；S3 scope/disposition）

```json
{
  "question_id": "Q...", "request_id": "U...", "task_id": null,
  "task_revision": null, "draft_revision": 1, "version": 1,
  "scope": {"level": "parts", "part_ids": ["d_b"]},
  "question": "b.txt 怎么处理？", "reason": "conflict", "subject": ["Inbox/b.txt", "Archive/b.txt"],
  "options": [{"option_id": "O...", "key": "A", "label": "b.txt 不移动",
               "actions": [], "disposition": "skip", "warnings": []}],
  "status": "open", "answer_id": null, "superseded_by": null
}
```

`question_id/option_id/version/时间/状态` 本地生成；key 只用于展示兼容，不再是身份。`disposition=skip|continue|cancel_request` 明确空动作含义，cancel_request 只允许 request 级问题。read 授权和 hidden/overwrite 继续从本地检查后的具体 actions/对象产生，不能由模型自由填写 grant。

S2 先为现有问题生成稳定身份，scope 标 `legacy_scope_unknown`，不把未知范围解释为整个请求；无精确作用域时，禁止据此扩展写入或把未处理目标结掉。S3 才启用 scope/disposition/draft 模型参数和局部决定。

问题所需文件指纹在本地保存（无需全回填模型）；回答后仍重新预检文件与确认卡，问题指纹不能替代计划快照。新版本问题替代旧版本时原子更新 superseded。默认一个 Request 同时一个活跃问题；其他 Task 的问题可保持记录，但不由“最近一问”自动覆盖。

### 4.4 所有消息共用接收事实、精确回答与 outbox（S2）

Web 为**每次用户提交**在发出请求前生成 `client_message_id`；结构化按钮不再往输入框塞 A：

```json
{
  "client_message_id": "一次提交的UUID",
  "content": "选择 A：b.txt 不移动",
  "response": {"question_id": "Q...", "version": 1, "option_id": "O..."}
}
```

response 可空：普通请求、自由文本回答、修订、新请求、按钮答案统一经过此入口。前端提交前将 ID、规范化 payload、会话保存到持久 pending submission；失败、刷新和超时重试复用同 ID，明确修改正文/选项则生成新 ID。清除只能发生在拿到 receipt 或用户显式放弃未发送提交之后。不按正文相同去重：用户有权有意再发一次相同请求。CLI 默认每次新命令生成 ID，显示于运行回执；`--message-id` 用于精确重试，命令参数有变化必须换 ID。

唯一事实源为 TaskStore 的 `message_receipts`：root/session/client_message_id 唯一，保存规范化 payload_hash、不可变原始 payload、receipt_id、run_request_id、可空 answer_id。所有消息都有 receipt；answers 只补充问题消费关系。payload 规范化按 API 版本、字段默认值、Unicode 原样正文、response 精确值定义，不能把不同正文通过 trim/自然语言归一误合并；首次创建后同一客户端重试沿用同版 canonicalizer。

事务严格顺序：

1. 先检查当前调用者有权访问该 root/session。不得先跨域查询 message_id。
2. 查询该隔离域的既有 message_receipt。若存在，先比较 payload_hash 和规范化 payload：相同则返回原 receipt/run/turn（turn 尚在建则返回同 run_request_id 的 queued），**不再检查问题是否 open 或当前 task revision**；不同则返回 `IDEMPOTENCY_CONFLICT`。这是 Q1 已答、出现 Q2、任务后来修订后仍能重试的关键。
3. 只有首次接收，在 `BEGIN IMMEDIATE` 中重复查询唯一键防竞态，然后验证当前问题/选项/版本。response 为空则仅校验消息本身，不假设它回答任何问题。
4. 精确答案执行 `questions.open→answered` CAS、写 answers；普通消息无答案记录。两者都在同一事务写 message_receipt 与唯一 durable run_request，随后提交。取消等终态已存在时首次续做请求不得开启写运行；可记录 no_run 的状态回执。
5. 唯一键竞争时回读既有 receipt 按步骤 2 比较，不生成第二个运行。`QUESTION_ALREADY_ANSWERED/STALE` 只对不同 message_id 的首次提交适用。

TaskStore 与 UI state.sqlite3 是两个事务域：`run_requests.receipt_id` 唯一，answer_id 可空；UI turns 的 run_request_id 唯一。outbox worker 补建同一 UI turn，再以 lease/epoch CAS claim 执行权；UI/前端数据库仅为投影，不能另外接收或消费一份答案。事务崩溃前无 receipt 则重试首次接收；已提交 receipt、未建 turn 则补建；已运行但结果不明标 interrupted 并核对日志，不自动重启写入。租约过期不等于可重放执行。

精确答案原子接收不代表已执行；receipt 返回 accepted/queued/running/ended/interrupted 及原运行引用。重复 POST 永不追加模型调用、预算消耗或文件操作；轮询原运行可继续更新展示。所有“零重复执行”验收都以动作日志与临时文件结果核对。

`POST /api/conversations/{cid}/messages` 的 root/session/request 由服务端解析，客户端不指定越界；错误不泄露其他会话存在性。HTTP 状态：200/202 返回同一回执；409 表示首次提交的过期/已答/幂等冲突；422 表示无效参数。


### 4.5 自由文本、CLI 与新请求（S2 保守兼容；S3 完整输入关系）

所有自由文本先经 4.4 持久接收/去重，再进入一次常规模型调用；接收事实不意味着消费问题或获得授权。S2 仍用现有 start/revise/ask/finish 解释关系，不自动消费旧字母或沿用未确认授权；无法安全区分时保守停止并指出需要哪条任务/问题，不承诺局部选择已经可续做。

S3 通过现有控制动作携带可选 input_resolution（仅容量原型通过后启用）：`kind=answer|revise|resume|new_request|independent_query|pause|cancel_request`、question_id/request_id。模型只解释当前用户文字，本地核实引用与允许状态转换；不增加单独分类请求，不给所有普通工具重复加字段。首个写入/敏感读取前必须解析关系，已有只读事实仍可复用。

- answer：消费明确问题并继续原请求；文字 hidden/overwrite 只形成候选决定，仍需具体权限卡确认；文字不能授予敏感读取权。
- revise：经现有 preview_edit/确认改变目标或放宽约束；声明本身不修改权限。
- new_request：新 Request，不继承旧选项/敏感 grant；旧问题暂停，保留显式返回入口。
- independent_query：查询其他事情，旧问题暂停而不是消费；后续字母不能任意匹配暂停问题。
- pause/cancel_request/resume：按第 5 节的完整状态与权限规则执行。

CLI 支持 `answer --question Q... --option O... --message-id ...`；交互字母由 CLI 显示问题时生成并保存 token，转换成精确 response。没有 token 的外部裸字母，不能判断是 Q1 重试还是 Q2 新答，按普通文本处理或要求指定问题。旧 Web 没 token 的按钮需要刷新，不能悄悄降级成字母授权。

## 5. 局部选择、暂停、取消与恢复（S3）

### 5.1 局部选择与精确结算

局部决定依赖 S1 的精确动作归属，按顺序：规范化并确定主要目标 → 查该目标/对象的有效决定 → 检查 task-wide 与局部限制 → 准备预检/具体确认/执行。b 的 skip 只阻止 b，不能阻止 a。S1/S2 尚无局部范围时保守拒绝多义映射，不能以“当前一个选项”结掉所有其他目标。

skip 的结果为目标 `by_choice/skipped_by_user`；其他目标已完成时整体可显示“已按你的选择处理”，必须列明 b 跳过，不能说 b 已移动。a 未处理且未跳过仍 unfinished。目标内只跳过部分对象时保存对象级 skip 并核验剩余对象；自然语言目标不能表达剩余范围时拆分目标，不能跳过整项目标。

选项 declared actions 精确约束涉及目标；按规范化实际 source/destination/文件身份核对，不以父目录相等替代准确目的地。`choices.record_choice` 改为 append-only decision_id/question_id/scope/revision/supersedes，按目标投影最新决定，两个局部问题可同时有效。目标/对象/契约变化使对应旧决定失效；不变目标可保留。权限 grant 和业务 skip 使用不同记录类型。

### 5.2 三种用户意图不得混为一谈

| 意图 | Request/Task/队列与问题 | 已执行事实和恢复规则 |
| --- | --- | --- |
| 跳过这部分 | Request 仍 active；只结算明确目标/对象；其他目标继续 | 不撤销；已做部分保留，未处理部分不能假完成 |
| 暂停/本次先不继续 | Request paused；待答问题 suspended；queued 续做暂停；Task 无新增授权 | 保留进度与任务预算；显式 resume 后复核任务版本、文件、旧问题有效性，再确认具体动作 |
| 取消整个请求/剩下都别做 | Request cancelled；关闭相关 open/suspended 问题；未运行 run_requests cancelled；Task 取消 | 保留已完成动作、日志、审计与预算；不自动撤销，不把剩余目标记为成功 |

全局取消与局部 skip 的明确标识由 S3 scope/disposition 表达，不靠空 actions 猜测。全局取消无需额外固定确认；这是缩小执行范围，不应再弹一次任务修改确认卡。精确取消按钮由服务端直接接收用户决定；自由文本由常规模型按原话提出 narrowing cancel，记录 source_message_id/用户原句与 actor。不得凭文件内容或模型自发的计划中止声明伪造“用户取消”。

现有 `TaskPatch.cancel` 保留用于已确认通用修订；新增 `TaskStore.cancel_from_user` 共用其取消校验与状态计算，但不调用通用 task_edit 的扩权确认流程，只允许 `cancel=True`，不可夹带 parts/limits/roots 修改。直接原子记录 task 新 revision、cancel actor 和 request cancellation_epoch。取消前无 Task 时只取消 Request，不为记录取消新建写任务。

取消一经接收先设置持久 `cancellation_epoch` 栅栏并关闭问题/未运行队列；执行器在进入确认、确认返回后、每个原子文件步骤前检查 epoch。取消提交与执行器检查并发时，以“检查后已经进入的一个原子步骤”为最小不可中断边界：允许该步完成并写日志，下一步停止。不能承诺瞬间打断已进行的 rename/日志写入。若当前执行器尚不能逐步检查取消，则 S3 不得启用“运行中取消”，UI 明确“已请求停止，将在当前安全边界停止”；待执行循环支持栅栏后才验收此功能。

待确认状态被取消时唤醒等待器为 interrupted(reason=user_cancelled)，不能复用允许结果。已经排队但后来才获得 worker lease 的运行须再次检查取消栅栏，不得写入。正在模型调用时可结束模型请求后停止后续工具，不能把收到旧答案再次激活请求。

### 5.3 取消后的报告、重试与恢复

- 零执行取消：flow=cancelled、result=no_result，显示“已取消”，无任务创建要求。
- 部分执行取消：flow=cancelled、result=partial，显示“已取消；此前已移动 a，b 未处理，可撤销”。保留 a 的 done 和真实机械核验，不能把 a 改成未执行，也不能聚合 fulfilled。
- 重发相同取消/答案 message_id：只返回原 receipt，不重启队列。新 message_id 提交已取消问题：拒绝，零写入。
- 恢复已取消请求必须用户显式要求；创建新执行 epoch 并经现有 confirmed revise/resume 预览取消状态与所需范围，不自动复活旧问题、旧确认或取消前 grant。进度/预算保留，文件重新预检，敏感读取授权重新取得，写入重新具体确认。
- 新请求为新 Request，不继承旧选项/敏感读取授权。撤销是独立显式用户动作，不等同取消，仍按原日志倒序且遇冲突不覆盖。


## 6. 目标—动作—证据的准确绑定

### 6.1 文件计划（S1 先封多认领；selection 新字段属 S4）

继续使用已有 `Action.part`，避免再新增同义字段；`goal_ids` 仅表示计划涉及集合，不能证明每步归属：

- 只有一个兼容目标时允许省略 part。
- 有多个兼容目标时，非依赖动作必须写 part，即使填写了 goal_ids 也一样。
- selection 是一个语义动作组，应新增可选 `Selection.part`，展开后的全部业务动作继承该 part；多目标混合批次用多个显式动作组或分计划，不按目录猜。
- 分类计划的完整 assignment 清单绑定一个 classify 目标，辅助 mkdir 与执行动作通过该来源契约绑定；不能把一个分类 P 任意认领到多个不同分类要求。
- 自动建目录可服务多个目标，存 dependency_for，不单独作为目标完成证明。用户明确要求“建目录”的 create 是业务动作，必须有主要目标。
- `_goal_steps` 对新协议不再回退 `step.goal_ids or plan.goal_ids`；旧计划显示兼容，但多目标模糊绑定不得新增成功证明。
- 完成证明保存 `task_id, goal_id, goal_contract_hash, plan_digest, step_ids, receipt_id`。版本变化时，描述/条件/限制相关契约变化即使 ID 不变也不能复用旧证明；不变目标可按现有规则保留。

机械准确归属不代表自然语言语义准确：模型仍可能把动作错误标到 g2。计划详情展示“目标→动作”便于审核；评测必须验证用户要求的独立文件结果，不能只断言 model-written part 与 receipt 自洽。

### 6.2 同类去重与专用执行器（S4，字段容量闸门）

在 `DeduplicateArgs`、`ApplyDedupArgs` 增加可选短字段 `part`，与 Action.part 同名同义；省略时只有唯一 receipt 消费目标才自动绑定，否则返回 `AMBIGUOUS_GOAL`，错误附候选短 ID。FindDuplicates 的 scan 仅产生事实，不产生写权限；scan 通过 `scan_id` 精确绑定，执行器核实 scope/指纹。

runtime 传显式目标给 `_check_write/constraints/日志/receipt`，整个调用使用同一份 resolved goal，不能在 after 阶段重新用 active_goals 猜。回执必须记录 task、goal、contract hash、scan 身份、已执行/未执行/结果不明步骤、保留对象版本。近似版本复核继续保留。

去重不必立即搬进 file_plans 执行器。统一的是执行契约：prepare→validate→confirm→execute/journal→verify→receipt。去重仍负责重复关系、保留策略与扫描范围；通用计划不能仅回收一个文件就伪造 dedup 完成回执。空去重回执须记录扫描覆盖与结果为空，避免“无动作”被当作任意目标完成证明。

### 6.3 只读证据与登记前观察（S4，字段容量闸门）

给一次读取强塞全局 active_goal 会误绑，因此先区分“收集证据”和“目标认领证据”。read/search/profile 返回本地 evidence refs；S4 选择在现有 finish_task 增加紧凑 `evidence_bindings=[{part, refs:[E...]}]`，或扩展 report_findings 的分组 part（二者择一实现，不同时暴露重复入口）。若 S5 启用再由 finish_turn 兼容承载，S4 不依赖它。候选未通过容量原型时不增字段，保持多 find 的未归属证据不能通过，明确保留功能限制。

本地核实 refs 属于同会话资料库、内容版本/位置有效、读取权限合法、与当前目标类型兼容；同一证据可以支持多个语义问题，但必须逐个显式声明，不能自动传播。相关性与答案正确性继续留 semantic review，不因 ID 有效就视为正确。

登记前观察保留为 request 事实，start_task 只在唯一兼容目标时自动采用；多 find 必须在终止绑定，不能把所有旧片段分给所有目标。修改目标后旧 evidence 可作为候选事实展示，但需要重新 claim，不能沿用旧目标证明。敏感文字缓存与会话 grant 分开，缓存命中不产生读取授权。

## 7. 流程、结果与验证状态分离

不建议只新增一个 answered 特例掩盖组合任务。新增独立维度，旧 status 在过渡期由投影产生：

```json
{
  "flow_state": "active | waiting_input | ended | interrupted | cancelled",
  "result_state": "answered | fulfilled | partial | no_result",
  "verification": {
    "mechanical": "not_applicable | pending | passed | failed | unknown",
    "semantic": "not_required | unreviewed | accepted | rejected"
  },
  "next_action": "none | answer_question | confirm_result | recover | retry"
}
```

目标级保留 `done/review/unfinished/not_needed/by_choice` 旧映射，同时新增结构维度；任务聚合有确定 reducer，模型只能声明未完成、不能提高 verification。完整条件仍由核验器计算，读答案的语义默认 unreviewed 不等于执行失败。

| 事件 | flow/result | 验证与用户动作 |
| --- | --- | --- |
| 普通解释完成 | ended/answered | mechanical=not_applicable；next_action=none |
| 有有效引用的只读回答 | ended/answered | mechanical=passed，semantic=unreviewed；网页仍已回答，无强制确认 |
| 缺少必要证据却给出任务回答 | ended/partial | 不能因只读而自动 answered；明确证据缺口 |
| 写计划全部执行并核验 | ended/fulfilled | mechanical=passed；分类等 semantic=unreviewed 时确认结果 |
| 明确跳过 b、a 已完成 | ended/fulfilled | b 为 skipped_by_user，报告明确按选择处理；不能写 b 已移动 |
| 仍有待答问题 | waiting_input/partial 或 no_result | next_action=answer_question，不能称最终完成 |
| 用户整体取消且零执行 | cancelled/no_result | next_action=none；旧问题与未运行队列关闭 |
| 用户整体取消且部分执行 | cancelled/partial | 保留已执行事实与撤销入口；不得fulfilled |
| 用户暂停 | interrupted/partial 或 no_result | next_action=none；显式恢复才续做 |
| 某目标写失败、其他成功 | ended/partial 或 interrupted/partial | 保留成功目标；报告实际失败，不自动回滚 |
| inflight 结果不明 | interrupted/partial | mechanical=unknown；next_action=recover |

保留当前 `reporting.display_state` 产品决定：只读 review 显示“已回答”，发生写入才提示确认结果。UI 不展示这些内部枚举。注意“本轮未写但任务历史写过”的状态按任务整体仍可需确认，不能只看当前 run。

评测变更必须新版本并双算：保留现有 status 四维判定及历史 13/15、7/8 原结果；新增 v4 读取 flow/result/verification 分维度判定。只读已回答不能要求语义必须接受才算流程结束，但必须独立检查答案与证据正确性；缺证据、假完成、限制违规仍失败。切换前冻结 expected semantics，列出每个旧失败如何映射，不允许为了提高通过率放宽到“显示已回答就通过”。

## 8. 确认来源与结构化工具结果、错误与恢复

### 8.1 S1 必须先保留的最小确认契约

先扩展本地确认结果 `ConfirmationOutcome={decision:approved|declined|timed_out|interrupted, source:web|cli|policy, confirmation_id, manifest_key, reason}`，无新增模型 schema。`allow()` 布尔兼容包装仅在 approved 返回 True；执行器通过 result API 读取来源再生成错误/报告，不能从 False 倒推用户拒绝。

Web 15 分钟等待超时为 timed_out；CLI EOF 为 interrupted(reason=eof)，KeyboardInterrupt 为 interrupted(reason=user_interrupt)；仅显式 n 是 declined。网络/页面断开只有服务端有可验证终止事件时才标 interrupted；普通浏览器关页不保证及时可知，继续等待直至 timed_out，不能虚构“检测到断连”。确认答案 POST 以 confirmation_id 幂等保存：显式拒绝的响应丢失后重试返回原 declined，不接受新 y 覆盖既有结果。

首批只做**本轮**拒绝拦截：对规范化实际动作清单生成 denied_manifest_key，含稳定 source 身份（没有稳定ID则精确来源路径）、effect、实际 destination、覆盖/隐藏影响；去掉随机 plan_id、描述和自动回收目标随机后缀。文件版本/指纹保存在被拒明细与具体 confirmation_digest 中，不进入拒绝比较键；否则同对象内容变化会错误清除用户拒绝。明确 declined 后缓存于 run ledger，同轮同计划或同 denied_manifest_key（换 ID 也相同）拒绝再次弹卡，返回 USER_DECLINED。文件变更不清除拒绝。timed_out/interrupted 也停止当前自动重试；用户显式重新尝试才可建立新确认，重新预检。不同清单可重新确认，但不得通过不相关措辞变化伪装成新清单。

此项不做自然语言相似度或跨轮语义等价判断；后续若要跨轮持久拒绝指纹，另行评测，不把该完整项目塞入 S1。部分已执行动作保留并可撤销，后续 declined/timed_out/interrupted 仅停止剩余部分，不抹去执行事实。多卡确认“本轮都允许”的现有语义保留，但取消栅栏、硬安全限制与已明确拒绝的同清单不得被它覆盖。

### 8.2 S6b 逐工具扩展结果

扩展现有 ToolResult，不另建并行结果系统：

```json
{
  "status": "ok | error", "text": "模型必要摘要", "result_id": "R...",
  "data": {}, "references": [{"kind": "evidence", "id": "E..."}],
  "operation_refs": ["journal:run:sequence"],
  "progress": {"completed": [], "remaining": [], "unknown": []},
  "error_code": null, "retryable": false,
  "recovery": {"action": "none | replan | reindex | ask | reconcile | retry", "target": null},
  "display_ref": null
}
```

完整 data/progress 存本地，发模型只给必要计数、引用和 bounded 摘要；避免既写 text 又把同份内容发两遍。status 保留 ok/error，局部失败通过 error+progress 表达，不一次重写所有 status 判断。references 和 operation_refs 必须由本地工具/执行器产生并解析校验，模型不可提交假日志身份。

错误最小分类：INVALID_ARGUMENTS、AMBIGUOUS_GOAL、QUESTION_STALE、IDEMPOTENCY_CONFLICT、OUT_OF_SCOPE、CONSENT_REQUIRED、TASK_REVISION_CONFLICT、PLAN_STALE、LIBRARY_BUSY、USER_DECLINED、CONFIRMATION_TIMEOUT、BUDGET_EXCEEDED、EXTRACTION_FAILED、PARTIAL_EXECUTION、OUTCOME_UNKNOWN、INTERNAL_ERROR。拒绝/超时/中断来源在 S1 已保留，S6b 只是将其统一投影为 typed error，不能等到本批才恢复入口信息；retryable 表示能在满足条件后重试，不授权自动重复弹卡或自动执行。

迁移顺序：先纯 report_findings 副作用 → read/search 引用 → 去重回执与部分失败 → 其他工具。旧字符串集中 adapter 保留 ERROR 前缀解析，已迁移工具不得继续写 runtime.final_answer；控制器统一消费 result_id，重复登记幂等。after 不再靠所有 artifact 增量猜哪次工具产生何物，但迁移期可以 shadow 比对差异。

局部失败与恢复规则：

- 每步使用稳定 action_id/manifest digest，事实源仍是操作日志与文件状态，不能只依据 ToolResult.status。
- 已完成且有确定日志的动作不重做；pending 重新预检；unknown/inflight 停止自动重放，先 reconcile，不能以目标同名/同内容存在断言成功。
- UI 消息发送失败不改变执行事实；保留现有 on_event 容错。
- 部分成功不自动全量回滚；向用户给出完成范围、未完成范围、撤销入口。撤销仍按日志倒序且遇冲突不覆盖。
- 跨工具无进展以结构化 error_code+目标/对象+已验证事实变化判定；新随机 plan_id、同证据重复 ID 不算进展。新增证据内容与真正完成动作才算进展，不靠降低 max_steps 掩盖问题。

## 9. 检索 fallback 与覆盖证明

保留现有全文 BM25 与抽取缓存。先解决覆盖可见性，不把向量数据库当默认答案。

search_content 的内部 data 增加：

```json
{
  "scan_id": "S...", "scope": "Notes", "index_revision": "...",
  "enumeration_complete": true, "indexed_files": 10, "failed_files": 1,
  "empty_files": 0, "unsupported_files": 2,
  "pending_ocr_units": 3, "ocr_failed_units": 0,
  "extract_complete": false, "matched_files": 7, "returned_files": 5,
  "candidate_truncated": true, "query_terms": ["..."], "strategy": "bm25"
}
```

extract_complete 指“该范围可读对象抽取是否完成”，不能等同语义找全。统计当前按单次 OCR delta 产生，不能直接将本次 skipped=0 当全库完成；应维护每份文件/页 pending、failed、unsupported 的索引状态，从状态汇总。enumeration_complete=false 或抽取失败时不能给出绝对“全库没有”。

策略选择仍由同一次 Agent 模型决定，本地限制预算与事实：名称/后缀走 search_files，数量/完整清单走 inventory/selection；内容先 BM25，零命中或语种明显不同可让模型生成一组原文/翻译关键词、同义词，使用一批 query 执行并按 file version 合并；候选去重后读上下文确认。不要加一个独立“先判断是否跨语言”的 API。

新增 batch query 可选参数必须以删去旧冗长说明抵消 schema；若字符预算不足，先复用多次 search_content，但总查询数不变。默认最多一轮扩展（例如最多 3 个查询），消耗现有 query budget；OCR 续跑最多在已分配预算内一次，不循环到“看起来找到”为止。预算耗尽报告覆盖缺口，不能暗中扩大范围或调用外部搜索。

“找全”启用显式 coverage ledger：范围内候选文件枚举 → 抽取可用性 → 每个对象已审/排除/未审理由。top_k 只供候选发现，不是全集。大库无法在预算内遍历时返回“已核对 X/Y，其余未核对”，不能承诺找全；不为了找全突破 500 项计划限制或自动分批执行。read_file 默认 PDF 前两页继续明确标注，全文检索片段不等于模型读过整篇。

向量/多语言 embedding 另开 C3 决策，须用冻结跨语言集合对比召回、时延、依赖和隐私开销后批准；本期不预设成功率收益。

### 9.1 S7 覆盖账本版本与固定样本成本验收（R8）

coverage ledger 的键为 root/session/request + scope_selector_hash + enumeration_snapshot_id + index_revision；每个审查条目再绑定 file_id/content_version/extraction_version/query_strategy_version。目录新增/删除/移名改变枚举快照，内容变更使对应已审结论失效；不变对象可继承已审结果，但必须记录继承来源并重算分母。旧快照已审10/10不得用于新增文件后的10/11并声称完整。语义相关性结论与抽取完成分别记录。

普通检索复用现有枚举/缓存计数，不为显示百分比额外逐篇阅读。只有“找全”请求建立对象审查 ledger；快照变动则显示“资料库已变化，新增/变更对象尚未核对”，不偷跑到全量读完。预算耗尽直接报告缺口，不固定追加“还要不要继续”；用户主动续做仍使用任务累计检索/阅读预算。

以下为冻结样例规格和**计划预算示例**，不是实测结果，也不承诺耗时/费用；S7实施时逐项填实际 query/read/OCR/cost/cache_hit/已审/总数/未审原因。用临时合成文档或真实原件副本，禁止写 data/real：

| 样本 | 资料/触发 | 预算示例 | 必须验收的事实 |
| --- | --- | --- | --- |
| CN→EN | 12份英文资料，中文问主题 | 原查询1+扩展最多3（合计4次），上下文读取≤6 | 记录语言扩展前后召回与费用，不能仅以词命中当相关 |
| CN→CN | 12份中文资料，同主题 | query≤1/read≤5 | 正常路径不固定翻译或额外分类调用 |
| 无词项且完整 | 8份均完成抽取 | query≤2/read≤3 | 只证明本次策略未命中，不宣称不存在相关文档 |
| 无词项且OCR未完 | 8份含3份扫描文档 | 单次OCR20秒，最多一次预算内续跑 | pending/failed单元准确，不能报全库无结果 |
| 后几页才相关 | 第4页含唯一细节 | query≤1/read≤2 | 全文索引命中，read_file准确page/offset，默认前两页不冒充全文 |
| 超过top_k | 30份候选，top_k=5 | 普通查询≤1；找全按剩余任务预算 | returned/truncated与已审5/30区分，不能冒充找全 |
| 扫描间库改变 | 已审10/10后加1份并改1份 | 不自动追加阅读 | 新快照分母11，1新增+1变更待审，旧10/10不得复用成完整 |

示例成本按实际配置价格乘缓存命中/未命中/输出 tokens，OCR时间单独计；离线脚本成本仅报告本地时长/调用数，不捏造模型费用。语言扩展召回与覆盖成本分别出表；首先保存可量测基线，不承诺固定提升百分比。

## 10. 缓存、提示预算与硬启用闸门（S0 交付，后续逐片重测）

本次在临时 TaskStore 实测：sensitive_files=False 为 **30,956**（余 355）；True 为 **31,047**（余 264）。最终上限 31,311 不变。测量复用当前测试公式：SYSTEM_PROMPT + 空任务 runtime.guidance + JSON 序列化工具 schema；不拿动态历史无限增长冒充静态协议容量。

新增离线原型脚本 `docs/repro/routing_schema_budget.py`，只深拷贝运行时生成的 schema，在临时目录创建 TaskStore，不改生产文件、不调用模型：

```sh
.venv/bin/python docs/repro/routing_schema_budget.py
```

| S1 候选修改 | 原文/位置 | 候选保留语义 | 实测序列化差额 |
| --- | --- | --- | --- |
| 删除过时图片禁读说明 | tools/fs_read.py 的 file_info：`图片不能用 read_file，要用它。` | read_file 的现有 OCR 说明保留；file_info 负责元信息 | −20 |
| 收紧既有 Action.part 描述 | file_plans.py：`此动作完成的部分 ID；一个计划完成多个同类部分时填写` | `动作的部分ID；多个候选部分时必填` | −10 |
| 精简 goal_ids 描述 | PrepareFilePlan.goal_ids 的既有说明 | `计划涉及的部分ID；多目标动作仍须part` | −16 |
| 总计 | 不新增字段，不删安全约束 | 为严格归属与能力一致性服务 | **−46** |

S1 候选实测 False **30,910**、True **31,001**，余 401/310。该结果只证明 S1 现有字段候选可容纳，**没有证明 finish_turn、input_resolution、scope/draft、dedup.part、evidence_bindings 等后续扩展能容纳**；不得把这些数字用作全量协议的最终测量。

S2 的 message_id/question_id/option_id、receipt/outbox/epoch/时间/数据库字段都由本地和客户端生成，不进入模型参数；提示无需新增 UUID 说明。当前建议实施范围止于 S0–S2，因此无需以未经验证的删减承诺为其背书；本轮方案审核不构成用户已授权实施。

S3–S5 每一候选启用前必须提交：完整 schema 快照、逐字段差额、具体删改来源与语义保留测试、两配置最终字符结果、纯聊天/只读/提问/写任务脚本请求数比较。候选删除/合并顺序限于：SYSTEM_PROMPT 与 TASK_GUIDANCE 重复的任务登记/收尾说明；AskUser 与 TOOL_DESCRIPTIONS 重复的选项说明；FinishTask.summary/reply 的重复展示说明；S5 替换时删去旧 ask_user/finish_task 的模型 schema，不同时暴露两套终止 schema。每条原安全规则须有明确保留位置，权限卡/隐藏/敏感/预算/证据核验语义不可删除。只有提交实际差额才能计入容量，不预估“应该够”。

**停止线：任一配置 >31,311、候选同时暴露新旧终止 schema、关键安全语义无保留证明、正常路径固定增加一次 LLM 请求、或脚本回归失败，均禁止开启对应扩展。** 无法容纳则交付 S1/S2 本地正确性切片，S3/S4/S5 保持关闭；不得提高 cap，也不得靠删安全约束上线。方案复审不等于越过这道未来实现闸门。

静态 SYSTEM_PROMPT/工具排序/schema 每协议版本固定；动态快照仍只有一份，不存回历史。默认不按状态切工具，权限在本地校验。不要恢复旧快照累积来追缓存。记录每次候选的首轮/续轮 tokens、cache hit/miss、费用、P50/P95；token 下降不代表费用下降，真实模型修复率与费用只有另行授权验收后才能判断。共享文字缓存不保存会话授权，trace 不保存凭据、敏感全文或隐藏推理。

## 11. 数据库迁移、单一权威与开关矩阵

### 11.1 数据表与事实源

全部接收/交互事实放 TaskStore SQLite：

| 表 | 唯一约束与用途 |
| --- | --- |
| message_receipts | PK receipt_id；UNIQUE(root,session,client_message_id)；不可变 canonical payload/hash、api_version、run_request_id、可空 answer_id；所有输入共有事实源 |
| requests | request_id；root/session/source_receipt 唯一；S2 最小请求，S3 扩展草案与 paused/cancelled/epoch |
| questions | question_id/version/status；root/session 隔离；S2 稳定身份、S3 scope/disposition；唯一活动问题约束 |
| answers | answer_id；UNIQUE(question_id,version)；receipt_id 唯一；只有精确答案/显式解析的文本回答创建 |
| run_requests | run_request_id；receipt_id 唯一；answer_id 可空；lease/execution_epoch/status；唯一执行交接事实 |
| protocol_meta | DB schema version、迁移阶段、校验结果 |

UI state.sqlite3 的 turns 只有 run_request_id 唯一投影，不另行写 questions/answers。oplog/manifest 保持文件执行事实源；receipt 不替代日志。`legacy pending_choice` 在 S2 迁移完成后仅由 questions 生成展示投影，不再独立可消费或双写竞争。兼容 adapter 也只能调用新 repository/CAS，不保留一条旧字母直接消费路径。

三个版本明确区分：DB schema_version 控制表迁移；session.protocol_version 控制模型工具协议（v3/v4）；api_version 控制消息 payload 与 canonicalizer；feature flags 是部署启用配置。回滚模型 adapter 不等于回滚数据库事实源。旧会话刷新升级其展示 token，不能重新创建无身份的新活跃问题。

### 11.2 迁移与回滚

显式 migrations/PRAGMA user_version，使用 SQLite backup API（含 WAL）在临时副本演练，再事务 DDL/回填。问题回填只从同 root/session 最新有效等待结果建立一个 stable legacy question；未知范围 `legacy_scope_unknown`，不自动当全局，缺映射不扩大授权。不批量把历史所有问题变活跃，不从旧字母制造新 grant。

旧 task.status/checks/评测结果保留，S6 懒投影新维度并记录 projection_version。旧模糊多目标计划可只读展示，未执行计划必须重建明确归属；已执行模糊计划保留实际日志但冻结其多目标证明，不自动判完成，不重复执行已确定动作。只允许在事实确认后补精确映射，不通过改历史目标描述“修复”证明。

每一迁移阶段有持久标记：not_started/copying/validated/authoritative。只有 validated 后单事务切换权威，启动见 copying 状态只读恢复/续迁移，不让两套状态同时接收。回滚旧 binary 如不能识别新 schema/权威标记必须启动拒绝或只读，不得退回旧 pending_choice 写入。

新增表首期不 DROP 旧列；备份只用于未发生新写入时的迁移恢复，绝不能用旧 DB 覆盖新答案、决定或文件执行账本。strict_goal_binding 一旦上线不能关回危险证明路径；若必须退版则冻结新写入、保留精确问题权威，继续只读查询/显式撤销。恢复执行先重新核对文件与日志。

### 11.3 允许组合与安全降级（R7，S2 交付）

记 G=strict_goal_binding，Q=structured_answers（包含统一 receipt/outbox repository），L=local_scope，T=strict_terminal。S1 的确认来源保护不可关闭成旧“False=拒绝”语义。S1兼容期的无精确问题token字母不能新增read/hidden/overwrite选项授权；这是未升级客户端的安全降级，不宣称其完整交互已修复。

| 会话/客户端/开关 | 读取与写入权威 | 允许行为与回滚去向 |
| --- | --- | --- |
| 旧会话+旧 API/旧按钮，G=1 Q=0 T=0 | 迁移前旧模型；S1 严格归属；旧历史只读 | 过渡版普通文字可用；已知旧字母串题未宣称修复，禁止据未知答案新增授权；需先升级 S2 才可发布精确按钮能力 |
| 旧或新会话+新 API/新按钮，G=1 Q=1 L=0 T=0 | message_receipts/questions/run_requests 唯一权威 | S2 基线；问题 token 精确；未知局部选择保守阻塞；普通正文路径暂保留 |
| 旧会话+旧按钮，G=1 Q=1 | 新 repository；旧 pending_choice 仅投影 | 无 token 的按钮返回刷新提示，零授权；无 message_id 的旧 POST 返回 upgrade_required，不能绕过幂等入口 |
| 新/旧客户端对同 ID 重试，Q=1 | 同一 api_version canonical receipt | 有访问权后原样返回既有 receipt，不检查后来的问题状态；相同 ID 改内容冲突 |
| G=1 Q=1 L=1 T=0 | S3 scope/决定权威+原 v3 终止 | 容量闸门通过后允许局部选择；不依赖强终止；旧 scope_unknown 不自动升级全局 |
| G=1 Q=1 L=1 T=1，新协议会话 | 新 finish_turn schema；同一交互 repository | S5 过闸；旧 ask/finish 不暴露给模型，回放经 adapter 只写新 repository |
| Q=1、旧 v3 会话、服务全局允许T | session.protocol_version=v3，T对该会话实际关闭 | 不在一轮中途切 schema；下一轮经显式协议迁移后才用新 surface |
| G=0 且允许任何新写入/核验新证明 | 不允许 | 启动拒绝或只读，不能降级回多认领 |
| Q=0 且 DB 已 authoritative 或 T/L=1 | 不允许 | 启动拒绝或只读；不重新消费旧 pending_choice |
| 新 API/new response 发送给 Q=0 服务 | 无安全适配 | 426/409 upgrade_required，不能忽略 response 后按 content=A执行 |

上述表覆盖旧/新 API、会话、按钮与关键开关组合。S3/S4 新字段候选若未过容量闸门，L/专用 claim 开关为0；可读旧状态而不可解释为新授权。每片发布须对矩阵允许组合做 HTTP/重启/迁移中断测试，并对禁止组合做启动负向测试。

## 12. 文件级实施清单与每片独立发布门

| 切片 | 文件与改动 | 独立门槛/回滚 |
| --- | --- | --- |
| S0 | tests/test_protocol_v3.py、test_answer_replays.py、docs/repro/routing_schema_budget.py；冻结正文提问/当前P0/完整基线、环境与迁移样本 | 不改生产；OCR根因隔离；所有后续candidate逐项字符账本 |
| S1 | completion.py、file_plans.py、verifier.py：已有part精确化、旧模糊计划冻结；prompts.py/fs_read.py仅候选−46说明改动；safety/confirm.py、webapp.py、web_confirm.py、registry.py：本地确认来源与同轮拒绝manifest拦截 | P0复现不成立；批准/拒绝/超时/EOF响应不混；两配置候选cap通过；G=1；故障时只读，不回退危险证明 |
| S2 | task.py、新 interactions.py、outcomes.py：message_receipts/questions/outbox单权威；webapp.py/web/app.js/main.py：所有消息ID、精确response、持久重试与CLI；asking.py/history.py：legacy投影adapter | 四类消息全套重试/崩溃、Q1→Q2与revision后幂等；无额外模型字段；DB迁移validated→authoritative；不能退回旧消费器 |
| S3 | choices.py、asking.py、finishing.py、task_runtime.py、file_plans.py、task.py、webapp.py：局部scope/draft映射、输入关系、cancel/pause/resume及每步epoch栅栏 | 依赖S1+S2；容量闸门；两项原xfail到该片才要求转pass；取消不假完成；异常时L=0保持身份权威与已答事实 |
| S4 | policy.py/constraints.py/tools/dedup.py/registry.py：专用part贯穿；finishing.py或findings.py一种evidence绑定；selection.part、contract hash | 每个新增字段候选cap；同类去重与多find的正反向测试；可独立不启用新增能力，不能恢复猜归属 |
| S5 | loop.py/task_runtime.py/asking.py/finishing.py/prompts.py：统一TurnEnd与finish_turn；普通正文错误仅一次修复 | 旧schema不并存；纯聊天/只读/提问/写入无固定额外LLM请求；容量两配置；T可关闭回v3终止，但Q/G不回滚 |
| S6a | task.py/intent.py/verifier.py/reporting.py/outcomes.py/web/tasks.js/eval/judge.py | 状态纯reducer与旧结果双报；不把取消映成完成；只读UI“已回答”保留；投影版本可回退 |
| S6b | registry.py/task_runtime.py/findings.py/retrieval.py/file_plans.py：typed results/错误、进展与恢复 | S1确认来源不重新解释；逐工具shadow/切换；局部失败与unknown结果不重放 |
| S7 | retrieval.py/extract.py/ocr.py/eval/retrieval_set.py/reporting.py：覆盖版本账本、多查询与有限fallback | 版本变更不复用已审结论；固定样本成本与覆盖表；无召回比例承诺 |
| S8及每片 | tests/*、eval/*_cases.py、迁移/故障注入/真实浏览器验收、交付记录 | 全局安全回归；临时数据；真实模型另行费用授权；新目录不覆盖旧证据 |

S1/S2 可独立上线，不依赖 S3–S5 新模型接口。S2 刻意不承诺局部选择/普通正文提问缺口已解决，保持保守阻塞；S3 才打开局部继续，S5 才解决结构性终止出口。所有切片的具体回滚都须遵守第11节单权威与精确证明不降级原则。

不编辑已有未提交 README/HANDOFF 等用户文件；正式实施后再按实际代码与验证结果更新，不提前宣称完成。

## 13. 验收矩阵与发布门槛

| 场景 | 必须观察到的结果 |
| --- | --- |
| 只做 g1，显式声明 g1/g2 但省略 part | 准备阶段拒绝；两目标绝不一起通过；补 part 后 g1 通过、g2 未完成 |
| 正确一个计划完成两个目标 | 每步准确归属，可一张卡确认；一目标 undo 后只该目标失败 |
| 两个 dedup 目标分别执行 | part 精确绑定，两个独立回执；省略多义 part 零写入并给可修错误 |
| 一个空去重、另一个非空 | 空扫描只证明对应目标，不伪造别的目标完成 |
| 登记前问 b，选跳过，再移动 a | b=by_choice，a=done；文件结果正确；不要求先创建任务才能提问 |
| a 未处理，选跳过 b 后直接收尾 | a=unfinished；不能“按选择”全部结算 |
| 两个连续局部问题 | 第一个决定保留，第二个只影响其作用域，不能全表覆盖 |
| 四类输入：普通请求/自由文本回答/修订/精确选项，响应丢失后重试 | S2：都只有一个receipt/outbox/turn；同ID改内容冲突；不按相同正文误去重 |
| Q1重试前task revision改变或进程重启 | S2：访问检查后返回Q1原receipt，不再校验open/version，不新增模型/写操作 |
| Q1 回答请求超时后重发，当前已 Q2 | 同 message_id 返回 Q1 原 receipt；Q2 不被消费；最多执行一次 |
| 同问题双线程/双进程不同答案 | 唯一一个 CAS 成功，另一 409；日志与实际操作无重复 |
| CAS 成功后、UI turn 创建前进程退出 | outbox 补建同一 turn；答案不丢失、不双消费 |
| 执行中进程退出、未知 inflight | 不自动重放；报告 unknown，核对账本后恢复 |
| 旧问题/版本变更/跨会话/任意 option_id | 零授权、零写入、准确错误；不泄露其他会话 |
| 自由文字回答/新请求/临时查询 | 明确关系；新请求不继承选项；文字不能授权敏感读取 |
| 普通聊天/只读提问/结构化回复 | 无任务登记；严格终止路径成功；无固定额外模型请求 |
| 普通正文含“是否”但仅解释 | 不按关键词误转问题；显式 reply 正常显示 |
| 需要用户决定却输出正文 | 终止协议错误按上限修复；不伪造选项、不未经决策执行 |
| 读后登记两个 find | 显式 evidence claim；不猜主题分配；缺证据目标不完成 |
| 多答案相同引用/证据过期 | 多 claim 可行但语义仍未验证；过期证据不能通过机械检查 |
| 只读 answer 与直接 reply | 均已回答，无多余确认卡；缺证据/错误答案独立失败 |
| 部分操作成功后工具 error | 正确展示已完成与剩余，可 undo；无日志成功不得算进度 |
| 用户拒绝/超时/CLI EOF/中断/确认响应丢失 | S1：declined/timed_out/interrupted来源准确；同轮同manifest拒绝不重弹；POST重试回原决定；零未授权写入 |
| 登记前全局取消、部分执行后取消、暂停后恢复 | S3：取消不新建Task；partial事实保留；取消关闭问题/队列；暂停不消费问题；恢复重新预检/授权 |
| 取消与排队/执行步骤/确认同刻竞争 | S3：epoch拒绝新步骤，最多已进入原子步完成且记日志；不谎称瞬时打断；旧答案不能重激活 |
| 中文查英文/零命中/OCR 未完/要求找全 | 有界扩展、覆盖缺口明确；top_k 不冒充全库；预算不重置 |
| 敏感检测开/关、隐藏项、no overwrite、500上限 | 保留原产品规则，扩大范围仍具体卡片确认，>500不自动分批 |
| 10轮续做/协议切换/历史回放 | 一份当前快照，tool exchange 成对，旧任务可读，用户限制和撤销事件完整 |

分片离线门槛：新增确定性验收与所有不涉及待隔离环境故障的既有测试全绿；S1先修当前多认领P0，S2修精确重试串题，S3才要求两个原strict xfail改为新API/局部场景真正pass；S1/S2保留不属该片的已知限制且不得宣称全修；新增P0无xfail；OCR 4 项必须完成根因隔离并在支持环境通过，不能带“全绿”声明发布。静态提示两配置≤31,311，JS语法和 diff检查通过；临时库做升级/回滚、重启与并发测试。真实浏览器验收必须实际点选、刷新、失败重试和恢复，不以源码测试代替。

真实模型验收在获得单独费用授权后进行：冻结新场景和期望，先现有 regression/holdout/parts/answer/product/office/ocr，再独立新场景，关键 8–12 组至少 3 次；沿用总费用 cap、新结果目录，不覆盖旧证据。所有越权、未经授权覆盖、错误完成证明、重复写入必须 0；出现即阻断发布，不能用总通过率抵消。一般成功率/额外请求数/缓存/费用与同模型配置基线比较，样本有限应给置信区间。工程目标：正常清晰请求不增加固定模型调用，协议纠错率不劣化超过 5 个百分点、同套件中位费用不增超过 10%；这是启动检查线，超过则分析阻断/回滚，不承诺小样本统计显著。

可观测事件最小字段：request_id、question_id、task/revision、goal、plan_digest、action/result/receipt、input_relation、transition_from/to、error_code、completed/remaining/unknown 计数、repair_count、使用量/缓存。记录 structured_answer/terminal 缺失率、问题过期率、幂等命中、局部 skip 外溢、ambiguous_goal 拒绝、错误证明数、检索覆盖与未抽取数。开发者详情展示引用链，普通用户只看所做内容、失败范围、下一步与撤销。

## 14. 风险与实施前待确认项

1. 终止工具切换可能影响模型正常聊天习惯。先 shadow 统计、脚本/供应商 schema 兼容，再小范围切换；一轮内 schema 不变。不能靠无限纠错执行严格协议。
2. 目标精确绑定会增加格式纠错，这是必要的正确性成本；用 part 短 ID、唯一目标推导、selection 继承减少负担，不能重新允许多认领来换成功率。
3. 草案目标与正式目标映射可能写错，需具体对象核对与独立文件结果评测；不声称本地能证明全部语义。
4. 两库 outbox 引入恢复复杂度，必须有故障注入与恢复命令。它只解决消息交接，不能解决文件系统和 SQLite 的原子性。
5. 状态拆分可能被误读为改判分，需要旧结果原样留存、判定器版本和独立答案检查；当前 UI 决策保持不变。
6. 检索覆盖统计可能很昂贵。先复用枚举/缓存与页状态，只在找全请求维护逐文件 ledger；不每次正常检索全量打印名单。
7. 费用增长与缓存失效必须实测；静态字符上限是硬门槛，无法容纳字段时缩减重复描述/分离本地字段，不提高上限。
8. OCR 环境失败、历史数据中 legacy pending_choice 的真实分布、供应商严格终止 schema 支持仍需在 S0 验证；不擅自把不确定性写成“已解决”。

本方案已通过独立产品复审，初审 R1–R8 的设计意见已逐项关闭。通过范围为本文的产品契约、发布依赖与验收门槛；建议首批为 S0–S2。S3–S5 仍须满足容量与行为闸门，后续实施仍须完成各片测试、迁移/恢复、真实模型及浏览器验收。本结论不代表生产代码已修改、现有测试全绿或产品已验收。


## 15. 独立产品审核 R1–R8 的逐项响应

| 审核项 | 正文实质修订 | 负责切片/完成证据 |
| --- | --- | --- |
| R1 幂等顺序 | §4.4：访问检查→既有receipt/payload比较→首次才状态校验/CAS；revision与Q2不能破坏旧回执 | S2；§13含跨revision/重启重试与零新增运行 |
| R2 普通消息 | §4.4、11.1：message_receipts覆盖所有输入，answer_id可空，receipt/outbox同事务；持久前端重试ID与CLI规则 | S2；四类输入故障矩阵 |
| R3 取消/暂停/skip | §5：三类独立转换、用户收窄取消免额外固定确认、Task取消兼容、队列/问题/epoch栅栏、部分执行报告与显式恢复 | S3；§7 reducer、§13竞争/报告/恢复用例 |
| R4 发布顺序 | §1、12：S0→S1 P0与最小确认→S2身份/幂等→S3局部；S5独立闸门；各片回滚明确 | 当前建议实施仅S0–S2，7–11人日；全部23–39，未沿用原过小承诺 |
| R5 协议容量 | §10：实际基线30,956/31,047；S1原型逐项−20/−10/−16，候选30,910/31,001；未来新字段不虚构测量，未过闸延后 | 脚本docs/repro/routing_schema_budget.py；只临时schema，无生产改动；S0及每扩展片硬门 |
| R6 确认来源与重弹 | §8.1、12：S1即传输approved/declined/timed_out/interrupted；同轮规范化manifest明确拒绝不重弹；部分事实保留 | S1固定用例；完整跨轮语义等价拒绝仍后续独立项目 |
| R7 单写权威/矩阵 | §11：三种版本分开；questions唯一消费源、legacy仅投影；矩阵覆盖客户端/会话/API/G/Q/L/T；禁止组合拒启/只读 | S2迁移中断与回滚演练；不退回旧消费路径 |
| R8 检索成本/版本 | §9.1：ledger绑定scope/index/file/extraction版本；固定7样本和预算示例、变更失效与实际量测字段 | S7；成本/召回分表，预算示例未冒充实测 |

修订未修改业务代码、既有用户文档或评测期望，没有新增付费调用；方案设计已由同一独立产品经理复审通过。审核记录保留初审退回、逐项关闭证据及本交付版 SHA256；实施尚未开始，不得表述为“已改代码且验收无误”。
