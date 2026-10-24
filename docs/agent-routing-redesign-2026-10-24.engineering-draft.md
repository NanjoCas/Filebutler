# FileButler 路由与任务协议改进方案（工程初稿）

日期：2026-10-24（Asia/Taipei）  
作者角色：资深工程师 Agent  
状态：工程初稿，等待独立产品经理审核；仅提出改造方案，未实施生产代码。

## 1. 决策摘要

建议保留单 Agent 与协议 v3 的主体：模型解释需求、选择语义策略，本地控制器管理流程与权限，现有执行器负责写入、日志与撤销，核验器判断事实能证明哪个目标。不增加独立意图分类模型调用，不把每种业务编成固定工作流，不默认动态切换工具 schema。

核心缺陷是“关系没有表达完整”：问题与回答、局部选择与目标、动作与完成证明、已回答与已验证之间仍有模糊地带。首批应先修正确性，再改架构整洁度。

| 批次 | 首要交付 | 性质 | 预计工程量 |
| --- | --- | --- | --- |
| A0 | 固定当前失败、建立数据/协议基线、隔离 OCR 基线失败 | 必做前置 | 1–2 人日 |
| A1 | 问题 ID、回答幂等、登记前作用域、局部选择、明确终止入口 | 首批必做 | 4–6 人日 |
| A2 | 强制精确动作归属、同类去重显式目标、只读证据绑定 | 首批必做，可与 A1 分支开发但集成顺序发布 | 3–5 人日 |
| B1 | 分离流程/结果/复核状态，保留现有网页“用户下一步”语义 | 第二批 | 2–3 人日 |
| B2 | 结构化结果/错误、局部失败与恢复、去重通用契约 | 第二批，分工具迁移 | 3–5 人日 |
| C1 | 检索覆盖与有界 fallback、可观测性补全 | 可延后 | 2–4 人日 |
| C2 | 收尾回归、HTTP/真实浏览器验收、迁移演练 | 各批均做，额外收尾 | 2–3 人日 |

合计约 17–28 人日，含联调和验证，不是交付日期承诺；首批 A0–A2 约 8–13 人日。单人串行、不新增外部服务；模型兼容性、迁移数据复杂度和 OCR 根因可能增加工期。每批单独可评审、可发布，不要求一次重写。向量检索、持久索引、SSE、通用非空文件夹回收、完整统一执行器、等价拒绝指纹属于独立后续项目，不作为本次首批依赖。

必须保留的产品边界：具体操作权限卡、可撤销、原子不覆盖、隐藏文件风险分级、敏感检测默认关闭且开启时读取只能明确点选授权、单计划最多 500 项且不自动分批、用户限制不可悄悄放宽、预算与既有缓存策略、31,311 字符模型可见静态协议上限。模型声称已完成不产生完成证明；任务范围估计、问题作用域、证据归属均不产生权限。

**批准实施的建议范围：先只做 A0–A2，阶段验收通过后再排 B、C。** 本文审查通过仅代表方案可执行，不代表代码已经通过验收，也不代表所有自然语言情形都正确。

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

主线程本次完整离线结果为 **431 passed、4 failed、2 xfailed、7 warnings**。4 项是 `test_ocr.py` 的图片、扫描 PDF、旋转照片识别，以及 `test_week1.py::test_read_tools`；见到 `Foundation._GenericObjCError error 0` 或空识别结果。只能确认当前执行环境 OCR 调用失败，根因尚未隔离，不能断言只是沙盒，也不能定性为路由回归。A0 需要建立可复现环境与平台失败分类；不得靠 xfail、删除断言或忽略失败把基线刷绿。

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

### 4.1 目标 API

首批引入一个稳定的终止工具 `finish_turn`，替换模型主路径中的 `ask_user` 与 `finish_task`，同时覆盖普通回复。不是新意图分类调用；模型在本来应结束的同一次响应调用工具，本地立即生成报告并停止，不再让模型为确认“结束成功”追加一次请求。

```python
FinishTurn = {
  "mode": "reply | ask | task",
  "reply": "用户主回复，可空",
  "question": AskPayload | None,      # 仅 ask；复用现有选项动作检查
  "summary": "模型补充，可空",         # task；沿用输出长度与detail语义
  "unfinished": [{"part_id": "g2", "reason": "..."}],
  "not_needed": [{"part_id": "g3", "reason": "..."}],
  "input_resolution": InputResolution | None
}
```

用枚举与本地 validator 保证字段组合互斥；如果模型供应商对 JSON Schema union 支持不稳，不必依赖复杂 discriminated union。`reply` 不创建 Task；已有未结任务时不能用 reply 绕过本地核验。`task` 必须有当前 Task；`ask` 必须先持久化问题再结束本轮。

旧 `ask_user`/`finish_task` 保留内部兼容 adapter 和 trace 解析，不同时长期暴露新旧三份大 schema。切换以会话协议版本/服务配置确定，单个会话在一轮内 schema 稳定。第一步可以先完成内部 `TurnEnd` 联合类型、让旧入口统一调用，最后原子切换模型 schema。

普通模型正文只作为过程说明；严格终止协议启用后，无工具直接结束属于 `TERMINAL_REQUIRED`，允许至多一次协议纠正（计入原预算、repeat/no-progress 保护），仍失败则保留草稿并报告“回复未形成完整交互，请重试”，不宣称 answered、不得执行后续写入。兼容模式下纯正文仍可保留历史行为，但打 `unstructured_terminal` 诊断，不猜它是不是问题；严格路径必须覆盖纯聊天回归后才能默认启用。

正式判断不扫描“是否”“要不要”“A/B”等词。正文提问错误靠强制显式终止结构与回放发现。该协议只能保证输出类型明确，不能保证模型不会把一个问题错误标为 reply；后者仍需语义评测及产品审查。

同一模型响应包含多工具时仍顺序处理；终止入口一旦成功，后续调用返回 `TURN_CLOSED` 且不执行，所有 tool_call/result 保持成对。持久化失败不展示可点击问题；已经发生的写入仍按操作日志报告，不声称本轮没有动作。

### 4.2 Request 与登记前问题

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

### 4.3 持久化问题与选项

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

问题所需文件指纹在本地保存（无需全回填模型）；回答后仍重新预检文件与确认卡，问题指纹不能替代计划快照。新版本问题替代旧版本时原子更新 superseded。默认一个 Request 同时一个活跃问题；其他 Task 的问题可保持记录，但不由“最近一问”自动覆盖。

### 4.4 精确回答、版本与幂等 API

Web 选项不再往输入框塞 A；请求体使用：

```json
{
  "client_message_id": "浏览器重试复用的UUID",
  "content": "选择 A：b.txt 不移动",
  "response": {"question_id": "Q...", "version": 1, "option_id": "O..."}
}
```

`POST /api/conversations/{cid}/messages` 扩展现有 NewMessage；response 可空，task/root/session 从服务端会话解析，客户端不可指定越界。`content` 仅展示，不参与按钮授权解释。HTTP 返回原 turn_id/run_id 或准确 409/422。

同一事务中使用 `BEGIN IMMEDIATE`：校验会话和版本 → 检查 client_message_id 唯一性 → CAS `open→answered` → 写 answer → 写 durable run_request。相同 message_id 和相同 payload 重试返回原 receipt/turn；相同 message_id 不同 payload 返回 `IDEMPOTENCY_CONFLICT`；同问题不同请求二次回答返回 `QUESTION_ALREADY_ANSWERED`；旧版本返回 `QUESTION_STALE`。不能仅依赖现有 Task revision checkpoint，它不能防同 revision 的双消费。

TaskStore 数据库与 UI state.sqlite3 是两个事务域，不假装统一原子：问题库的 `run_requests` outbox 为事实源，UI turn 用 `run_request_id` 唯一索引幂等创建；worker 启动时 claim lease 后执行。进程重启扫描 queued 请求，运行过但结果不明标 interrupted，先核对日志；不得把 running outbox 自动重跑写操作。崩溃在问题已消费、turn 未创建时，只补建同一请求的 turn。两线程/两进程竞争用数据库约束和 CAS，不只靠前端按钮禁用或进程内锁。

### 4.5 自由文本、CLI 与新请求

无 response 的文本不立即生成任何授权。模型在现有常规调用的首个有效控制动作（start/revise/finish_turn，或可兼容的工具上下文）说明：

```json
{
  "kind": "answer | revise | resume | new_request | independent_query",
  "question_id": "Q... 或 null", "request_id": "U... 或 null"
}
```

本地只验证引用、状态与允许转换，不判断文字含义。首个写入或敏感读取前必须解决输入关系；与关系无关的普通读取可以继续。普通工具不用全部重复加该字段；控制器从本轮已解析上下文获取。不同模型/协议 adapter 的具体承载方式在 A0 用 schema 与脚本回放验证。

- answer：校验问题仍有效，保存原话，继续原 Request/Task；文字同意 hidden/overwrite 仍仅形成候选决定，必须在具体写入确认卡明确确认。
- revise：用户更改目标或放宽约束仍走现有 preview_edit/确认；输入关系声明本身不能修改任务。
- new_request：生成新 Request，不继承旧问题选项、read grant、chosen scope；旧问题标 suspended/superseded 并保留可显式返回入口。
- independent_query：可以查询其他内容，旧问题暂停而不是消费；后续返回原任务必须明确引用，不让 A 落到任意一个暂停问题。
- resume：恢复既有任务和预算，核查文件与执行账本；不生成授权。

CLI 优先支持 `answer --question Q... --option O... --message-id ...`；交互字母输入由 CLI 在显示问题时绑定本地 token 后转换为结构化答案。同一个裸字母经外部客户端重发，无法从内容上判断是旧答案还是新答案，这是信息不足，不承诺自动分辨。兼容裸字母仅在当前一个有效问题且客户端有对应显示 token 时接受；无 token 的脚本裸字母按普通文本处理或要求指定问题。Web 必须从首批起提交精确关联。

## 5. 选择范围与目标结算

现有 `_within_choice` 在整计划范围检查，应改为“先精确归属，再对作用域内动作应用决定”：

1. 规范化动作和目标归属；未知/多义归属先拒绝。
2. 查该目标/对象的有效决定；b 的 skip 只阻止 b，不能阻止 a。
3. 按 task-wide 限制、局部限制、scope 与动作检查。
4. 本地准备计划、预检、具体确认、执行。

问题选项能有多个 declared actions；这些动作约束该选项涉及的目标，不约束所有未来任务。选项执行核对用规范化实际 source/destination/身份，不用“目标父目录相等”宽松判定替代准确匹配。目录目标展开后持久化对应具体文件目标。

`choices.record_choice` 目前会覆盖此前所有 choice/skip。改成 append-only 决定记录（decision_id、question_id、scope、revision、supersedes），投影每个目标最近有效决定；两个局部问题能同时有效。任务修订只保留未变化目标的有效决定，改变对象/目标/授权范围必须失效并重新确认。

取消/跳过是 `skipped_by_user`，不是“文件操作已完成”；整体结果可标“已按你的选择处理”，报告列出跳过项。没有执行且没有明确跳过的 a 仍未完成。一次问题覆盖目标中的一部分文件时，应保存对象级 skip 并核验剩余对象；如果目标只有自然语言而无法表达剩余范围，先拆为独立目标，不能以一个 skip 结掉整个目标。

## 6. 目标—动作—证据的准确绑定

### 6.1 文件计划

继续使用已有 `Action.part`，避免再新增同义字段；`goal_ids` 仅表示计划涉及集合，不能证明每步归属：

- 只有一个兼容目标时允许省略 part。
- 有多个兼容目标时，非依赖动作必须写 part，即使填写了 goal_ids 也一样。
- selection 是一个语义动作组，应新增可选 `Selection.part`，展开后的全部业务动作继承该 part；多目标混合批次用多个显式动作组或分计划，不按目录猜。
- 分类计划的完整 assignment 清单绑定一个 classify 目标，辅助 mkdir 与执行动作通过该来源契约绑定；不能把一个分类 P 任意认领到多个不同分类要求。
- 自动建目录可服务多个目标，存 dependency_for，不单独作为目标完成证明。用户明确要求“建目录”的 create 是业务动作，必须有主要目标。
- `_goal_steps` 对新协议不再回退 `step.goal_ids or plan.goal_ids`；旧计划显示兼容，但多目标模糊绑定不得新增成功证明。
- 完成证明保存 `task_id, goal_id, goal_contract_hash, plan_digest, step_ids, receipt_id`。版本变化时，描述/条件/限制相关契约变化即使 ID 不变也不能复用旧证明；不变目标可按现有规则保留。

机械准确归属不代表自然语言语义准确：模型仍可能把动作错误标到 g2。计划详情展示“目标→动作”便于审核；评测必须验证用户要求的独立文件结果，不能只断言 model-written part 与 receipt 自洽。

### 6.2 同类去重与专用执行器

在 `DeduplicateArgs`、`ApplyDedupArgs` 增加可选短字段 `part`，与 Action.part 同名同义；省略时只有唯一 receipt 消费目标才自动绑定，否则返回 `AMBIGUOUS_GOAL`，错误附候选短 ID。FindDuplicates 的 scan 仅产生事实，不产生写权限；scan 通过 `scan_id` 精确绑定，执行器核实 scope/指纹。

runtime 传显式目标给 `_check_write/constraints/日志/receipt`，整个调用使用同一份 resolved goal，不能在 after 阶段重新用 active_goals 猜。回执必须记录 task、goal、contract hash、scan 身份、已执行/未执行/结果不明步骤、保留对象版本。近似版本复核继续保留。

去重不必立即搬进 file_plans 执行器。统一的是执行契约：prepare→validate→confirm→execute/journal→verify→receipt。去重仍负责重复关系、保留策略与扫描范围；通用计划不能仅回收一个文件就伪造 dedup 完成回执。空去重回执须记录扫描覆盖与结果为空，避免“无动作”被当作任意目标完成证明。

### 6.3 只读证据与登记前观察

给一次读取强塞全局 active_goal 会误绑，因此先区分“收集证据”和“目标认领证据”。read/search/profile 返回本地 evidence refs；在 `finish_turn(mode=task)` 增加紧凑 `evidence_bindings=[{part, refs:[E...]}]`，或者扩展 report_findings 的分组 part（二者择一实现，不同时暴露重复入口）。建议先用终止时显式绑定，减少所有读取工具 schema 膨胀。

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
| 某目标写失败、其他成功 | ended/partial 或 interrupted/partial | 保留成功目标；报告实际失败，不自动回滚 |
| inflight 结果不明 | interrupted/partial | mechanical=unknown；next_action=recover |

保留当前 `reporting.display_state` 产品决定：只读 review 显示“已回答”，发生写入才提示确认结果。UI 不展示这些内部枚举。注意“本轮未写但任务历史写过”的状态按任务整体仍可需确认，不能只看当前 run。

评测变更必须新版本并双算：保留现有 status 四维判定及历史 13/15、7/8 原结果；新增 v4 读取 flow/result/verification 分维度判定。只读已回答不能要求语义必须接受才算流程结束，但必须独立检查答案与证据正确性；缺证据、假完成、限制违规仍失败。切换前冻结 expected semantics，列出每个旧失败如何映射，不允许为了提高通过率放宽到“显示已回答就通过”。

## 8. 结构化工具结果、错误与恢复

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

错误最小分类：INVALID_ARGUMENTS、AMBIGUOUS_GOAL、QUESTION_STALE、IDEMPOTENCY_CONFLICT、OUT_OF_SCOPE、CONSENT_REQUIRED、TASK_REVISION_CONFLICT、PLAN_STALE、LIBRARY_BUSY、USER_DECLINED、CONFIRMATION_TIMEOUT、BUDGET_EXCEEDED、EXTRACTION_FAILED、PARTIAL_EXECUTION、OUTCOME_UNKNOWN、INTERNAL_ERROR。拒绝与超时不得都当“用户拒绝”；retryable 表示能在满足条件后重试，不授权自动重复弹卡或自动执行。

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

## 10. 缓存、提示预算与工具说明

- 静态 SYSTEM_PROMPT/TASK_GUIDANCE/工具排序与 schema 每个协议版本固定；动态状态仍只有一份，不存回历史。不要恢复旧快照累积来追缓存数字。
- 不默认按流程隐藏/替换 schema；权限在本地执行门验证。可以记录合法下一动作短提示，但不能为每个新错误追加长规则。
- 删除 file_info 图片旧说明，统一“对象信息”与“OCR 读文字”职责；沿用单一 descriptions/FIELD_DOCS 来源，增加能力说明一致性检查。
- 每个新增字段的说明都在 schema 差额表登记；root/session/digest/time/result_id/outbox 等本地字段不暴露成模型参数。问题 token 原样由客户端/控制器承载，不要让模型生成 UUID。
- 空任务静态提示+schema 含敏感规则两种配置均 ≤31,311 字符；不得改 cap。当前 30,956/31,047 仅是文档基线，实施前由测试重新测量。
- 每次接口批次记录字符、首轮/续轮 tokens、cache hit/miss、费用、P50/P95 请求时间。不得把 token 下降直接等同费用下降；2026-10-23 单快照已证明两者可能相反。
- 不缓存会话授权到共享文字缓存，不在 trace 保存凭据、全文敏感内容或隐藏推理。

## 11. 数据库、迁移与兼容

新增表建议全部放 TaskStore SQLite，字段服务端管理：

| 表 | 主要索引/用途 |
| --- | --- |
| requests | PK request_id；root/session/source_message_id 唯一，保存轻量请求与草案 |
| questions | PK question_id；request/task/version；status 的 CAS；每 Request 至多一个 open |
| answers | answer_id PK；root/session/client_message_id 唯一；question_id 唯一消费；payload_hash |
| run_requests | run_request_id PK；answer_id 唯一；status/lease/worker/turn_ref，桥接 UI outbox |
| protocol_meta | schema_version、迁移完成时间、feature flags |

使用显式 migrations/PRAGMA user_version，不仅构造器 CREATE TABLE IF NOT EXISTS。迁移先本地备份 SQLite（使用 SQLite backup API，避免 WAL 漏失），在临时副本演练；DDL/回填在事务内，失败回滚。API serializer 与 DB schema 独立版本化。

旧 pending_choice：仅能从同 root/session 最新有效等待结果回填一个 legacy question；生成稳定 ID，保留原选项文本，task_revision 已知则校验。缺少目标范围一律标 legacy_scope_unknown，不自动当全局；需要新问题或显式映射后继续。旧纯历史问题用于展示，不批量回填成活跃问题。迁移不能从旧字母制造新的授权事实。

旧 Task.status/checks/决策保留原值，新增维度懒投影并存 projection_version；禁止重写历史文件结果或把 needs_review 批量改 completed。旧多目标模糊计划保持历史可见，恢复/重新核验需显式补绑定或重做预检；已经执行的文件不回滚，也不让旧模糊证明认领新目标。

兼容：服务端先发布接受新旧消息体的代码，再发布 Web 精确 response；旧按钮没有 token 时不按字母授权，提示刷新。旧模型控制工具 adapter 只用于旧协议会话/回放，生产新协议不全量同时暴露。API 409 返回“该问题已更新/已处理，请使用当前问题”，不暴露其他会话存在性。

回滚：各批 feature flag 分离（structured_answers、strict_terminal、strict_goal_binding、state_v4、typed_results、retrieval_coverage）；新增表向后兼容，不在首期 DROP/改旧列。关闭新终止 schema 必须同时恢复对应会话 adapter；精确问题消费/严格归属不可回滚到会造成串题或假完成的危险路径，必要时降级只读、暂停新写操作。数据回滚不得覆盖已经新增的写日志、问题答案和用户决定；备份用于迁移恢复，不能拿旧 DB 覆盖已执行文件状态。

## 12. 文件级实施清单

| 批次 | 文件 | 具体改动 |
| --- | --- | --- |
| A0 | tests/test_answer_replays.py、test_protocol_v3.py、新 tests/test_routing_baseline.py | 冻结正文提问回放、显式双目标误判、旧状态映射；隔离 OCR 失败、记录环境与命令 |
| A1-存储 | agent/task.py、新 agent/interactions.py、agent/outcomes.py | Request/Question/Answer repository、事务 CAS、outbox、版本；outcome 只引用 question_id；legacy adapter |
| A1-输入 | webapp.py、web/app.js、main.py、tests/test_web_ui.py | NewMessage response/message_id；按钮精确提交、失败重试复用 ID；两库 outbox 幂等；CLI token |
| A1-终止 | agent/loop.py、task_runtime.py、asking.py、finishing.py、prompts.py、history.py | 内部 TurnEnd/finish_turn；终止门；输入关系解析；保持 added_messages 与单快照；限制一次修复 |
| A1-局部决定 | agent/choices.py、file_plans.py、verifier.py | 决定按问题/目标追加；归属后检查选项；精确 skip 和目标映射；独立保留多个局部问题 |
| A2-写证明 | completion.py、file_plans.py、policy.py、constraints.py、tools/dedup.py、registry.py | 多目标必填 part；selection.part；显式专用工具 part；同目标上下文贯穿回执与日志 |
| A2-读证明 | finishing.py、verifier.py、task.py、tools/findings.py、artifacts.py | 目标证据 bindings；登记前 request 观察；契约 hash；旧引用兼容且不多认领 |
| B1 | task.py、intent.py、verifier.py、reporting.py、outcomes.py、web/tasks.js、eval/judge.py | 纯 reducer、双状态投影、新旧评测双报；保持网页标签产品语义 |
| B2 | tools/registry.py、task_runtime.py、tools/findings.py、tools/retrieval.py、file_plans.py、web_confirm.py、safety/confirm.py | ToolResult 扩展、错误类型、部分进度、确认结果来源；报告统一消费 |
| C1 | tools/retrieval.py、extract.py、ocr.py、eval/retrieval_set.py、reporting.py | 机器可读覆盖、文件/页状态、有限多查询融合、覆盖 ledger 与报告 |
| 各批 | tests/*、eval/*_cases.py、新 docs/协议迁移与验收记录 | 正反向/并发/崩溃测试；冻结行为与实际结果、迁移和回滚演练 |

不在本阶段改 README/HANDOFF 等已有未提交用户文件；正式实施结束后由负责工程师按实际结果另做文档更新，不提前宣称完成。

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
| 用户拒绝/超时/断连 | 区分来源，不自动重复弹窗或假设用户允许 |
| 中文查英文/零命中/OCR 未完/要求找全 | 有界扩展、覆盖缺口明确；top_k 不冒充全库；预算不重置 |
| 敏感检测开/关、隐藏项、no overwrite、500上限 | 保留原产品规则，扩大范围仍具体卡片确认，>500不自动分批 |
| 10轮续做/协议切换/历史回放 | 一份当前快照，tool exchange 成对，旧任务可读，用户限制和撤销事件完整 |

离线门槛：新增确定性验收与所有不涉及待隔离环境故障的既有测试全绿；两项原 strict xfail 转为真正 pass；新增 P0 无 xfail；OCR 4 项必须完成根因隔离并在支持环境通过，不能带“全绿”声明发布。静态提示两配置≤31,311，JS语法和 diff检查通过；临时库做升级/回滚、重启与并发测试。真实浏览器验收必须实际点选、刷新、失败重试和恢复，不以源码测试代替。

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
8. OCR 环境失败、历史数据中 legacy pending_choice 的真实分布、供应商严格终止 schema 支持仍需在 A0 验证；不擅自把不确定性写成“已解决”。

本工程初稿可进入独立产品审查。产品审查至少检查：局部决定符合用户意图、普通问答保持轻量、确认次数不增加固定负担、失败/恢复表达真实、授权没有扩大、只读状态没有强迫复核、首批范围可控。审查提出阻塞意见时必须修改方案并再次审核，不能仅把“待办”附到文末后宣称通过。
