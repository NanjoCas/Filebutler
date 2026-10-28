# FileButler 2.0 大版本更新方案

日期：2026-10-26（Asia/Taipei）
状态：方案已通过产品审核，**未实施**；第 4 节的决策项待用户选择。
基线：`master` `c217685`（路由改造 S0–S8 已实施，第 45/46 轮真实验收后）。
版本说明：「2.0」是产品版本号，与模型工具协议 v3、判定器 v3/v4、已退役的协议 v2 无关。

## 0. 这份文档怎么来的

- 用户要求：读 HANDOFF 与路由重设计文档，分析链路是否还要优化，参考 GitHub/网络上他人踩过的坑与经典解法；方案由资深工程师 Agent 撰写，Agent 产品经理审核，确认无误后回报。
- 过程：
  1. 两个只读调研：代码链路审计（对照 HANDOFF、`docs/agent-routing-redesign-2026-10-25.md`、第 45/46 轮 trace），外部资料检索（第 6 节）。
  2. 资深工程师写 r1 → 产品经理审核「有条件通过」，退回 R1–R8，其中 R1 为阻断项（删 `start_task.input_resolution` 会让打字回复后的去重卡死）。
  3. r2 修订 → 复审「有条件通过」，补 N1（waiting 口径）、N2（无回执路径的问题无法关闭）、N3（A0 需本方自有验收清单）。
  4. r3 吸收 N1–N3 → 终审**通过**，采纳「新场景在复验前冻结」的补充。
- 全程只读，没有改代码、没有调用付费模型。
- 同一晚另一个会话（docagent-3d）独立写了 `docs/agent-chain-optimization-2026-10-25.{engineering,product-review}.md` 与 `docs/repro/chain-audit-2026-10-25.py`。其中 A0（撤销库锁）、A2a（OCR 账本）两项经本方产品经理读代码独立确认成立，已吸收；其余内容未经本方审核。

### 0.1 更正此前的两处结论

1. 「影子比对 92 轮 0 次不一致，可以删」——依据不成立。`tools/registry.py:204`（工具未给 references 时用 artifact 差集补）与 `agent/task_runtime.py:653` 算的是同一个差集，0 次不一致是构造必然。删仍可删，理由应是「它从来不是独立校验」。
2. 「名称当路径 8 次」——实为 4 次失败调用（第 45 轮两遍 `real_classification` 各 2 次）。

## 1. 总判断

链路仍需优化，但不需要大改。第 45/46 轮共 641 次工具调用，失败 31 次（4.8%）；v4 pass^1 87.5%、pass^3 20/24（v3 84.7%、19/24）。

失败集中在两类根因：
- **模型自填的状态副本与本地事实不一致**（典型：任务里的自由文字问题与问题表脱节）；
- **报错没有给出路**（ID 写错不给候选、名称当路径不提示句柄、控制工具坏 JSON 返回英文原文）。

所以 2.0 的收益是：少无效往返、少一张无意义的 task_edit 确认卡、修掉一个重复提问缺陷、收敛旧路径和多套状态判断。**不以提高通过率为目标。**

| 顺序 | 切片 | 依据 | 人日 |
|---|---|---|---|
| 前置 | A0 撤销安全（库锁、原子不覆盖） | `safety/oplog.py:35-81` 撤销不拿 library_lock，exists 检查后 `Path.rename` 有覆盖窗口；`main.py` undo 走同一函数（中等严重度） | 0.5–1 |
| P0-C | 文档更正（0.1 节两处） | 代码确认 | 0.1 |
| P0-B | ID 与路径防呆、报错统一给出路 | 第 45 轮 ID 写错 4、名称当路径 4、英文 JSON 报错 5 | 1.5–2 |
| P0-A | 待答问题只认问题表 | 历史 8 次被拦调用/7 个运行，0 次促成真正提问；第 45 轮 `s5_nothing_by_choice` r1/r2 重复提问 | 2.5–3.5 |
| P1-A | 删 SYSTEM_PROMPT 两句重复 | −80 字 | 0.3 |
| P1-E | OCR 覆盖账本刷新 | `agent/coverage.py:37-38` key 不变时条目不刷新，pending 计数却更新 | 1–1.5 |
| P1-B | 旧路径收敛 | 影子比对自比；计划级 goal_ids 回退 5 处；strict_terminal=false 不是完整回滚 | 1–1.5 |
| P1-D | 状态显示单点推导 | ≥7 处各自判状态（严重度低） | 1–1.5 |
| P1-C | 评测 v2 | 第 44/46 轮各 1 次是评测选错选项被判失败 | 2–3 |
| P2 | 连续错误升级提示、执行与收尾同一响应（实验） | 证据弱 | 1–2 |

合计 9.4–13.9 人日（不含 A0）。

## 2. 关键设计

### P0-B：ID 与路径防呆（模型可见提示 0 字）

- goal_ids 不存在的报错补 `parts_text(task)` 候选（`agent/file_plans.py:744,795`，现在只有 part 报错给候选）。
- read_file 找不到文件时，在本会话已登记的 F 句柄中找唯一同名者，报错写「可能是 file:F…（路径）」，**只给候选，不自动改读**（`tools/fs_read.py:84`）。
- search_content 的 evidence 视图改为显示完整相对路径，复用 detail 视图的 `json.dumps(path, ensure_ascii=False)`（`tools/retrieval.py:327`），不截断，使 `names.shield` 能按完整文件名匹配（`agent/names.py:37`）；单页放不下时减卡片数而不截路径，并与 P1-E 的「实际展示数」同步。
- 部分 ID 只做大小写、`-`/`_`、全半角统一，唯一命中才接受并在结果注明；第 45 轮 4 次 ID 错误里只能修好 1 次（`organize-1`），其余靠候选；start_task 登记时校验规范化后 ID 仍唯一。先例：task_id 唯一前缀容错（`agent/task_runtime.py:430-438`）。这是对标识符的通用归一，不针对任何关键词。
- 新增 `registry.error_text(code, 说明, candidates, example)` 统一报错格式；控制工具坏 JSON 和参数错误改为中文，error_code 改为 INVALID_ARGUMENTS（现为英文原文加 CONTROL_REJECTED，`agent/task_runtime.py:190,227`）。

### P0-A：待答问题只认问题表（−295 字）

- 删 `StartTask.questions`、`ReviseTask.questions`、FIELD_DOCS 中 `revise_task.questions`（必须同删，否则 `_with_docs` 报 KeyError），以及 `agent/prompts.py:102`「无法确定的事放 questions」那句。
- **保留 `start_task.input_resolution`**：deduplicate / apply_dedup 不经 prepare（`tools/dedup.py:330,390`），删掉会让打字回复后的去重卡死（审核阻断项 R1）。
- Interactions 新增 `open_question(task_id)` 按任务过滤（`agent/interactions.py:367` 现无过滤）；`task_state(task, waiting)` 由调用方传入是否等待。
- **waiting 口径**：该任务存在 status 为 open 或 suspended 的问题即 waiting；replied 只在本轮等模型声明关系，不单独构成 waiting。
- 删两道写入闸门（`agent/task_runtime.py:379-380`、`agent/file_plans.py:858`）；`pending_choice` 只写不读，保留一个版本供回滚；`_resume_asked_task` 按回执对应的问题记录找任务。
- **无回执的库调用路径**：「上一轮有问题」以 `store.outcome().pending_choice` 为准（`agent/asking.py:84-87`）；本条消息不是精确回答时，写入前要求声明输入关系；模型声明 answer / new_request 时，运行时把该问题显式标为 replied / suspended，让它能关闭。这个声明是已有工具上的参数，只在上一轮有问题且要写入时检查，不增加固定模型调用。
- 旧任务里非空的 `unresolved_questions` 作只读「历史备注」显示，不参与状态；TaskPatch 收到该字段时中文拒绝；`web/tasks.js:98` 改只读。
- 代价：模型登记任务时写下的其他疑问记录会消失；按钮回答清空整个数组的问题（F2）随机制一起消失。

被删安全规则的保留位置（按路由方案 §10 要求）：

| 被删的规则 | 由什么保证 |
|---|---|
| 有待答问题时不开放写入 | 提问结束本轮，之后的调用返回 TURN_CLOSED（`agent/task_runtime.py:187-188`、`agent/asking.py:361`、`agent/finishing.py:266`） |
| 打字回答后不随意写入 | INPUT_RELATION_REQUIRED（`agent/task_runtime.py:295-299`）；去重经保留的 `start_task.input_resolution` 声明 |
| 无回执调用路径 | 上面「无回执的库调用路径」一条 |
| 权限 | 权限卡、隐藏文件分级、覆盖需同意、敏感读取授权均不变 |

外部参考：LangGraph interrupt 的挂起状态只存在 checkpointer 一处；12-factor agents 第 5 条（执行状态由事件推导）。

### P1-A：删 SYSTEM_PROMPT 重复句

删 `agent/prompts.py:31-32`（53、27 字），TURN_RULE 与须问清单里仍保留同样的意思。

容量合计 −375：提示从 31,048 降到 30,673（敏感检测开：31,152 → 30,777），余量 638 / 534。定稿实施前用 `docs/repro/routing_schema_budget.py` 实测确认（StartTask.questions 估 120–137 字）。

### P1-E：OCR 覆盖账本刷新（0 字）

`record_scan` 在 key 不变时也逐文件刷新抽取状态（OCR pending → indexed 时由 excluded 改为 unreviewed）；scan_id 纳入查询参数（`tools/retrieval.py:202`）。

### P1-B：旧路径收敛

- 删影子比对（`agent/task_runtime.py:649-655`），文档写明 references 是单次调用窗口内的 artifact 差集。
- 计划级 goal_ids 回退收敛为 `step_owners()`，只对旧计划回退；新计划保存时断言每一步都有 goal_ids。
- strict_terminal=false：先补 `test_legacy_schema_has_no_dangling_finish_turn_refs`。是否删除由用户决定（Q4）；删除时同步修订路由方案 §11.3 与 §12 的 S5 行，回滚改为 git revert 加数据只读兼容。删除前各片保持 T=0 测试（`tests/test_protocol_v3.py:212`、`tests/test_answer_replays.py:291`）通过。

### P1-D：状态显示单点推导

`states.display_of(states)` 取代 `reporting.display_state`；`agent/loop.py:286` 给每份报告写 display；`web/tasks.js`、`web/app.js` 只读该字段，旧 stop_reason 表仅作兜底。

### P1-C：评测 v2（只进新版判定和新场景，冻结场景不改）

- 选项匹配 v2：move 和 rename 归为同一效果族，目标路径仍精确核对；规则可带前提。
- 模型没给场景需要的选项时，发送冻结好的自由文字，并单独记 `option_not_offered`（不计通过，也不从分母剔除）。
- 报告 pass^1、pass^3、可执行数、未覆盖数、环境失败数。
- 补路由方案 §13 未覆盖的测试：空去重与非空去重、过期证据、纯回复只用 1 次请求、repair_count 统计。

### 不采纳 DeepSeek strict 模式

- 所有可选属性都要列进 required，仅属性名约 +1,541 字，可空类型包装再加约 2–4k（推断），远超余量。
- 与本地依赖的「没填 vs 显式 null」语义冲突（`agent/task_runtime.py:203-205`）。
- 只防得住 31 次失败里 5 次结构错误。
- 是 Beta 接口。

## 3. 实施顺序、门槛与费用

顺序：A0 → P0-C → P0-B → P0-A → P1-A → P1-E → **复验一** → P1-B → P1-D → P1-C（离线部分可并行）。

**A0 前置门槛（本方案自有验收清单）**：以下五种情况全部不覆盖已有文件——
1. 两个独立进程同时执行与撤销；
2. 检查之后外部新建了同名目标；
3. 原位置已被占用；
4. 只改大小写的还原；
5. CLI 撤销（`main.py:181`）走同一个加锁函数。

A0 以单独提交合入，经审核方复核后才发布 P0-A、P1-B 和做复验一。

**每片门槛**：
- 新增测试在修复前的代码上失败、修复后通过，不用 xfail；
- 在 Vision 可用的环境全量离线通过，或附根因隔离记录（本机历史 529 passed；另一会话沙盒 525 passed / 4 failed，失败为 3 个 OCR 用例加 `test_week1::test_read_tools`，报错含 `Foundation._GenericObjCError`）；
- 两种配置提示都 ≤31,311，写明实际数字；
- `node --check web/app.js web/tasks.js`、`git diff --check` 通过；
- P0-A、P1-D 做真实浏览器验收；
- P0-A、P1-D 用 778 个历史场景加 73 条真实运行记录做新旧状态逐条比对，允许差异逐条列出。

**复验一（单一总预算约 ¥1.84，软上限 ¥2.0，需另行授权）**：

| 项 | 遍数 | 估算 |
|---|---|---|
| regression / answer / s5 / parts / ocr | 3 / 3 / 3 / 3 / 1 | ¥0.57 / ¥0.15 / ¥0.39 / ¥0.09 / ¥0.04 |
| 新场景 5 个，串行执行 | 各 3 遍 | ≈¥0.60 |

新场景：`s6_doubt_at_registration`（两个候选对象，期望 mode=ask、零写入、waiting_input）、任务内提问后打字回答、evidence 视图后读文件、候选超过 top_k、两个连续局部问题。5 个新场景及其期望在复验一启动前冻结并单独提交，之后不改。

停止线：
- 任何越权、覆盖、错误完成证明、重复写入：立即停；
- 失败调用率超过 9.6%（第 45 轮 4.6% 加 5 个百分点）：停；
- 同套件中位费用增加超过 10%：停；
- 累计估算达 ¥2.0 不再启动新场景（在途最多超约 ¥0.05）。

P1 的付费复验另行申请。结果用新目录，不覆盖旧证据。

## 4. 待用户决定（★为推荐）

| 题 | A | B | C |
|---|---|---|---|
| Q1 待答问题 | ★删 questions 字段，以问题表为唯一来源（代价见 P0-A） | 另一会话方案 A1：保留字段、逐条绑定 | 维持现状 |
| Q2 名称当路径 | ★evidence 显示完整路径，找不到时给候选、不自动读 | 名字唯一时自动解析 | 只改报错 |
| Q3 影子比对 | ★删除 | 让工具自带 references，真实比一轮再删 | 保留 |
| Q4 strict_terminal=false | ★复验一通过后删，并改路由方案 §11.3、§12 | 立即删除 | 长期保留 |
| Q5 v4 接替 v3 退出码 | ★复验一中 v4 通过数不低于 v3、无「v3 过 v4 不过」、越权与覆盖类错误为 0 即切，v3 并列报告 | 现在切 | 继续用 v3 |
| Q6 测试基线 | ★本机 Vision 可用环境复测全量作为新基线 | 接受 525/4，附隔离记录 | — |
| Q7 回收的 375 字 | ★两种配置统一保留 300 字，可用约 234 字等新场景证明需要再用 | 立即加「找全」字段 | 加对象级 skip |
| Q8 跨请求沿用「已核对」（`agent/coverage.py:42-45`） | ★保留记录，但不计入本次「已核对 X 个」，显示「以前读过」 | 跨请求一律清空 | 维持现状 |
| Q9 A0 由谁做 | ★授权实施会话按 A0 验收清单单独提交，审核方复核 | 本方案实施者顺带完成 | — |
| Q10 付费复验 | ★复验一约 ¥1.84 | 约 ¥2.7，P1 后再一轮 | 暂不复验 |

另：非空文件夹整体回收（第 45/46 轮 31 次失败中 9 次源于此）属于产品规则变化，不在 2.0 链路优化范围内，另立方案单独决策。

## 5. 不做的事

| 不做 | 理由 |
|---|---|
| 提高 31,311 字上限 | 用户硬约束 |
| 删 `start_task.input_resolution` | 去重路径会卡死 |
| DeepSeek strict 模式 | 见第 2 节 |
| 从历史里删除坏 JSON | 4/4 次都在下一次请求恢复，删了反而损失审计记录 |
| 按状态动态切换或延迟加载工具 | 破坏前缀缓存，违反路由方案 §10；外部数据来自 77k token 量级，不可比 |
| 合并 revise_task 与 start_task | 第 40 轮字段嵌套导致报错 31 次、平铺后降到 5 次的教训 |
| 名称自动解析、ID 前缀或序号匹配 | 写入归属属于 P0 风险，只给候选 |
| 不经收敛直接删计划级回退 | 旧任务重新核验会失败 |
| 引入 LangGraph 或 Temporal | 现有回执、日志、权限边界已覆盖，只会增加迁移面 |

## 6. 外部参考

- Anthropic, Building effective agents（poka-yoke：改成必须绝对路径后零失误）：https://www.anthropic.com/engineering/building-effective-agents
- Anthropic, Writing tools for agents（有语义的 ID 少幻觉；错误要给正确示例）：https://www.anthropic.com/engineering/writing-tools-for-agents
- Anthropic, Advanced tool use（tool search、input_examples；工具少于 10 个收益不大）：https://www.anthropic.com/engineering/advanced-tool-use
- OpenAI function calling（枚举让非法状态无法表达、已知参数不让模型填）：https://developers.openai.com/api/docs/guides/function-calling
- OpenAI Agents SDK（StopAtTools、reset_tool_choice）：https://openai.github.io/openai-agents-python/agents/
- SWE-agent ACI（护栏：去掉 lint 解决率 18.0%→15.0%）：https://arxiv.org/html/2405.15793v3
- DeepSeek tool calls / strict 模式（Beta）：https://api-docs.deepseek.com/zh-cn/guides/tool_calls
- LangGraph interrupts（挂起状态只存一处、恢复时节点重跑须幂等）：https://docs.langchain.com/oss/python/langgraph/interrupts
- humanlayer 12-factor agents（第 5、7、8、9 条）：https://github.com/humanlayer/12-factor-agents
- Stripe 幂等键：https://docs.stripe.com/api/idempotent_requests ；Temporal 活动定义：https://docs.temporal.io/activity-definition
- Less is More（减少暴露工具提高函数调用成功率）：https://arxiv.org/abs/2411.15399
- τ-bench（pass^k）：https://arxiv.org/abs/2406.12045
- Anthropic, Demystifying evals for AI agents（评产出不评路径）：https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents

## 7. 验证方法（实施阶段）

- 每片：`.venv/bin/python -m pytest -q`（Vision 可用环境）；`docs/repro/routing_schema_budget.py` 测两种配置字数；`node --check`；`git diff --check`。
- P0-A、P1-D：按 `docs/repro/s6-v4-rejudge-2026-10-25.py` 的方式做 778 + 73 条新旧状态逐条比对；`eval/browser_acceptance.py` 加问题卡、打字回答、任务面板场景。
- 复验一：结果目录 `eval/results/policy_live_<date>_47_*`，汇总 v3/v4、错误类别、名称当路径与 ID 错误次数。
