# S5 路由改造问题分析报告

日期：2026-10-25（Asia/Taipei）  
代码基线：035567ce51a510d1e95ebcff6294132d7f748844，分支 optimize/completion-binding。  
性质：分析报告；未修改业务代码、HANDOFF、已有方案或评测期望，未调用付费模型。

## 1. 结论

S5 已把收尾入口统一，但真实模型暴露了三个不同层次的问题：参数结构容易写错、控制器拒绝了一种正常的回答声明、用户选择“不做”后的业务状态与旧评测不一致。不能把这些问题一概归因于删除 schema title，也不能把修改后的离线测试通过当作真实模型复验通过。

最新提交已经处理“提问字段嵌套”和“精确答案后声明 answer 被拒”两项。本次在当前代码另复现两项应优先修复的缺口：**已有任务用 reply 收尾时可能丢失主回复；非法 finish_turn 调用没有纳入无进展保护。** 此外，mode=reply 仍能装入正文选项，mode=ask 会静默忽略其他模式的字段，说明统一入口仍需补齐模式契约与边界测试。

建议先完成小范围的正确性修复和“不做”的业务契约，再冻结新版本做针对性真实复验。保留当前 title 默认关闭；现有证据不足以支持全量恢复 title，也不足以证明删除完全无影响。

## 2. 本次核查与证据范围

- 阅读最新 [HANDOFF](../HANDOFF.md)、S5/S4/S3 及后续修复提交、当前收尾/主循环/输入关系/报告/评测实现。
- 独立工程审查与产品/评测复算并行进行；主线程独立复跑关键回放与 A/B 复算。
- 阅读第 40 轮 24 个结果目录中的 manifest、report、judgement 和相关 trace/history。manifest 的生产代码快照一致，显式配置差异为 schema_titles。
- 当前定向离线测试：152 passed、5 warnings，覆盖 test_protocol_v3、test_answer_replays、test_interactions、test_cancel。
- 另有 4 个当前行为的最小确定性回放，均在临时合成资料库复现。它们用于证明当前缺口，不能计为修复成功；后续修复应改变相应断言。
- 未重跑完整离线套件；HANDOFF 的“478 passed”是历史记录。未运行真实模型、真实浏览器或用户资料操作，不能据本报告认定全部权限/交互边界已验收。

## 3. 第 40 轮实际说明了什么

A 为去掉 title；B 为保留 title。两组运行的是“平铺提问字段”修复之前的同一版产品代码，比较 title 开关，**没有比较 S5 改造前后**。

### 3.1 分母必须一致

| 指标 | A | B |
| --- | ---: | ---: |
| 原始报表通过 | 46/51（90.2%） | 48/50（96.0%） |
| 完整配对通过 | 45/49（91.8%） | 48/49（98.0%） |
| 完整配对模型请求 | 283 | 280 |
| 完整配对费用估算 | ¥0.93797864 | ¥0.93867496 |
| 完整配对失败工具调用 | 25 | 13 |

r1 B answer 的 ans_do_nothing 第二轮网络中断；后续 ans_skip_b 未运行。按 case 键能找到 50 对，其中只有 49 对双方完整执行。不能拿缺样的总请求数、总费用比较效率。完整配对费用仅差约 ¥0.00069632（0.074%），基本持平。

49 对中 45 对都过、1 对都败、3 对仅 B 过。独立配对假设下，精确双侧 p=0.25；固定 17 个场景重复、样本小和基础设施缺样限制推断。“没有显著差异”不等于已经证明两者等价。

| 真正不一致的完整配对 | A | B | 原因 |
| --- | --- | --- | --- |
| r1 / regression / dedup_cleanup | needs_review，失败 | done，通过 | A 多登记无法机械检查的 notes，触发人工复核 |
| r2 / answer / ans_do_nothing | answered，失败 | done，通过 | A 未建任务；B 建任务后按选择结算 |
| r3 / answer / ans_do_nothing | answered，失败 | done，通过 | 同上 |

因此 HANDOFF“多失败的三次都是 ans_do_nothing”不准确。A 全部 5 次失败为“不做”3 次、dedup_cleanup 2 次；B 的 2 次失败为网络中断 1 次、dedup_cleanup 1 次。

### 3.2 工具出错不等于增加一次模型请求

全部原始 trace 中，A 有 411 次工具调用、31 次失败；B 为 383 次、14 次失败。31/14 是失败工具调用数，同一模型响应可以含多个工具调用，不能直接解释成额外 31/14 次往返。

报表模型请求数为 301/285；trace 实际尝试为 301/286，B 有一次失败 API 请求未进入 report 的已完成 turns。失败次数分类如下：

| 工具 | A | B |
| --- | ---: | ---: |
| finish_turn | 12 | 5 |
| prepare_file_plan | 11 | 4 |
| read_file | 4 | 2 |
| start_task | 4 | 2 |
| submit_classification | 0 | 1 |

HANDOFF 的“嵌套提问 A8/B3”需要说明分类口径；不能把所有 finish_turn 错误都归为嵌套问题。失败还包括非法 JSON、幻造字段、scope/disposition 和精确答案关系检查。

原始证据：[r1 A answer](../eval/results/policy_live_20261025_40_r1_A_answer/judgement.json)、[r1 B answer](../eval/results/policy_live_20261025_40_r1_B_answer/judgement.json)、[B 网络中断 trace](../eval/results/policy_live_20261025_40_r1_B_answer/ans_do_nothing/turn_2.trace.jsonl)、[r1 A regression](../eval/results/policy_live_20261025_40_r1_A_regression/judgement.json)。完整复算脚本见第 8 节。eval/results 被 Git 忽略，以上证据链接仅在保留原始结果的本机可用。

## 4. 已暴露问题、根因与当前修复状态

### 4.1 提问参数嵌套：已改结构，尚未真实复验

原 finish_turn 用 question 对象包住整个 AskUser，AskUser 内又有 question 文本。模型要区分“外层问题对象”和“内层问题句子”，实际出现字段写到顶层或幻造 question_reason 等情况。

当前提交将 question 文本、reason、options 等平铺到顶层，合法旧嵌套形式由本地展开。这个修改减少了一层生成负担，也明确了 question 的含义。当前离线正反向测试存在；不能据此声称历史所有错误参数都已可接受，更不能据旧 Round 40 声称修复后纠错率下降。

位置：[finishing.py](../agent/finishing.py#L48-L67)、[兼容与检查](../agent/finishing.py#L141-L157)、[回归测试](../tests/test_protocol_v3.py#L172-L183)。

### 4.2 精确点选后声明 answer：已修正常兼容，尚未真实复验

精确按钮已由本地解析为答案。旧 declare_relation 只允许“尚未选择、需要模型理解的文字回应”声明 answer，模型在精确按钮后再声明一次就被拒绝。这个冗余声明没有扩大权限，不应该要求模型删字段再重试。

当前已将精确答案上的 answer 声明视为不重复应用的正常声明，保留既有决定。该错误跨工具在两组各发生 4 次；最新本地修复和测试已确认。

位置：[task_runtime.py](../agent/task_runtime.py#L267-L276)、[回归测试](../tests/test_protocol_v3.py#L186-L192)。

### 4.3 “不做”得到 answered 却被判失败：状态契约仍未解决

A 三次 ans_do_nothing 均保持文件原样，文件、回答、约束维度通过；失败来自冻结期望要求 done。A 未建 Task，精确选择“不移动”后直接回复，得到 answered；B 完成的两次先建 Task，再由 by_choice 结算为 completed/done。

当前代码和新测试仍允许前一路径以 answered 结束，最新平铺修复没有解决这个状态差异。问题在于业务决定依赖模型是否创建 Task，而非用户实际选择。

B 通过路径还有报告问题：零操作的 completed 会生成“已完成：文件已经符合要求，没有改动”，但文件没有满足最初“移到 Archive”的要求，实际是用户选择保持原样。应说“已按你的选择保持原样”，并保留选择证据。

建议给“不做”保存请求级决定与作用范围，区分“已按选择处理”“真正执行完成”“全部取消”“纯回答”；登记前后应产生一致的业务结论。不要为了过旧评测强迫创建虚拟 Task，也不要简单把 answered 加进旧期望。保留旧判定，冻结新的业务语义与独立文件/约束检查后再版本化判定器。dedup_cleanup 的 needs_review 也不能通过自动删除 notes 来放行。

位置：[冻结期望](../eval/answer_cases.py#L38-L53)、[无任务回复](../agent/finishing.py#L172-L179)、[零操作报告](../agent/reporting.py#L173-L175)。这是 S6 应优先补的状态/报告契约，不足以支持“模型理解能力下降”的结论。

## 5. 当前代码新增复现的缺口

本节 P1 表示应优先修复的回复/恢复问题；P2 表示协议边界或观测问题。本次未发现终止后继续执行的旁路，也不把这些问题描述为已发生未授权写入。

### P1-A：登记任务后的 reply 可能从主回复消失

触发：归档 a 后，模型用 finish_turn(mode=reply, reply=用户要求的额外解释) 收尾，不填 summary。

实际：本地核验通过，文件移动成功，stop_reason=done；额外解释不在 final，只在“补充说明”详情。原因是已有任务的 reply 分支把 reply 改作 summary，并将传给报告的 reply 置空。统一入口在保留本地核验时改变了同名字段的展示语义。

建议：reply 在 reply/task 两条路径都保持“用户主回复”的含义，本地事实仍先展示；summary 仍用于补充。长度超限应按现有任务回复规则明确拒绝或处理，不能静默转移。测试须断言用户请求的解释在 final 中恰好出现一次，而非只断言 done。

位置：[finishing.py:162–167](../agent/finishing.py#L162-L167)、[reporting.py](../agent/reporting.py#L159-L186)。

### P1-B：错误 finish_turn 没有进入无进展保护

触发：连续提交不同参数的非法 finish_turn，例如 mode=ask 缺少必要提问字段，每次只改变 reply 文案。

实际：task_repair_limit=6，仍能连续失败 8 次，第 9 次模型请求才成功。相同参数重复保护无法覆盖这种变体；NoProgressGuard 的 repairable_call 集合没有 finish_turn。“纯文字纠正一次”的限制只针对没有工具调用的回复，也不会限制工具参数错误。

建议：先将非法终止纳入现有无进展计数，覆盖参数、scope、证据绑定等无进展错误；S6 再细化机器错误码和恢复动作。已终止后的 TURN_CLOSED 不应再驱动修复。补不同参数连续错误、无新事实的交替读取、真实新证据重置、一次纠正后正常结束等回放。不能靠降低全局 max_steps 代替修复。

位置：[loop.py](../agent/loop.py#L232-L243)、[NoProgressGuard](../agent/repetition.py#L21-L38)。

### P2-A：ask 静默忽略其他模式字段，文档夸大核验范围

当前 mode=ask 分支提前返回，因此带不存在的 unfinished.part_id、假的 evidence_bindings 仍能成功提问，且 task.checks 为空。这不意味着提问必须执行终态核验；中间提问本来就应暂停。但模型提交的非 ask 字段不能悄悄丢弃。

建议：定义模式字段互斥，拒绝 ask 中的 task-only 字段，或者明确独立检查点语义。HANDOFF 的“已登记任务无论哪种 mode 都走本地核验”改为“reply/task 收尾会核验；ask 检查选项、保存问题并等待”。

位置：[finishing.py](../agent/finishing.py#L151-L160)、[asking.py](../agent/asking.py#L340-L354)。

### P2-B：调用 finish_turn 不保证提问语义一定正确

当前模型可以把正文 A/B 选项放进 mode=reply，系统仍返回 answered，没有 question_id 和按钮。本次合成回放已复现；它不代表已在修复后的真实模型观察到。

S5 可靠地识别“有没有用收尾工具”，无法仅靠 schema 判断“模型是否选对 mode”。现有正文选项测试只覆盖 bare text 被纠正，未覆盖错标 reply。建议补这类语义反例，给模型简短的正确 ask 示例，在真实复验中统计“需要选择却没有问题卡”的比例。不重新引入关键词问句判断，不承诺 schema 能证明自然语言意图正确。

### P2-C：错误文字和机器码不一致

终止后的调用被正确拦截，文字为 TURN_CLOSED，但回填结构化错误码可能仍为 CONTROL_REJECTED。建议统一，避免日志归因和后续恢复误判。该项属于观测缺口，本次没有发现终止后执行成功。

位置：[prepare](../agent/task_runtime.py#L180-L183)、[结果回填](../agent/loop.py#L226-L230)。

## 6. 提示词与工具负担的最新实测

按既有测试公式，使用空任务临时库测量 SYSTEM_PROMPT、guidance 和序列化工具定义，不含真实聊天历史与工具返回正文：

| 当前配置 | 字符数 | 相对 31,311 余量 |
| --- | ---: | ---: |
| 默认去掉 title，敏感检测关闭 | 30,830 | 481 |
| 默认去掉 title，敏感检测开启 | 30,934 | 377 |
| 实验恢复全部 title，敏感检测关闭 | 35,022 | 超出 3,711 |
| 实验恢复全部 title，敏感检测开启 | 35,126 | 超出 3,815 |

当前模型可见工具 19 个，工具 schema 共 28,133 字符；finish_turn 单个占 5,151。去掉 title 和重复说明确实腾出了空间，但新增 scope、draft、input_resolution、evidence_bindings 等又消耗了容量。因此统一工具数量不等于减少全部协议复杂度，当前总量依然约 3.1 万字符。

不建议仅凭小样本将全量 title 恢复到默认配置。应先改善 mode 契约、参数结构和错误恢复；如需研究 title，可另做局部标题/说明对照，并遵守预算与冻结条件。超过硬上限的全量 title 配置只作为已有实验条件解释，不能直接变成生产默认。

## 7. 建议修复顺序与验收

| 顺序 | 工作 | 必须观察到的结果 |
| --- | --- | --- |
| 1 | 修 P1-A 主回复保留、P1-B 有界终止修复 | 两模式的主回复均可见；变换参数也不能绕过无进展上限 |
| 2 | 补 mode 字段互斥和错误码 | ask 不静默丢 task 字段；TURN_CLOSED 机器码与文字一致 |
| 3 | 定义“不做/跳过/取消/纯回复”契约 | 登记前后业务结果一致；真实零写入；报告准确表达用户选择，不伪称原始要求已满足 |
| 4 | 整理 HANDOFF 现状 | 首行日期、最新复验、当前字符余量、失败归因与待办不矛盾，旧数字明确标历史 |
| 5 | 冻结修复版后针对性验收 | 先确定性回放，再另行费用授权后的真实模型；最后实际浏览器点选、重试、刷新、停止 |

最小真实复验建议覆盖：纯回答、登记前/后提问、精确点选后 answer、登记前/后“不做”、局部跳过、部分执行后取消、写入后额外解释、无效终止参数恢复、mode 语义选错。记录业务文件结果、状态/报告、额外请求、失败工具调用、费用和缓存，分别判定；不以文件正确掩盖回复缺失，也不以旧状态失败认定文件执行失败。

本次不执行付费复验。若后续授权，使用全新结果目录，保持旧 Round 40 和冻结期望，新增评测语义须单独版本化。

## 8. 可复现材料

当前缺口回放：[s5-current-audit-2026-10-25.py](repro/s5-current-audit-2026-10-25.py)。仅创建 TemporaryDirectory，使用假模型与合成文件；修复后断言应改变，不应为保持脚本通过保留缺陷。

A/B 复算：[s5-ab-audit-2026-10-25.py](repro/s5-ab-audit-2026-10-25.py)。只读本机第 40 轮原始结果，不调用 API；原始结果未入 Git，缺失时不能复算。

在项目根目录运行：

~~~sh
.venv/bin/python docs/repro/s5-current-audit-2026-10-25.py
.venv/bin/python docs/repro/s5-ab-audit-2026-10-25.py
.venv/bin/python -m pytest -q tests/test_protocol_v3.py tests/test_answer_replays.py tests/test_interactions.py tests/test_cancel.py
~~~

本次报告的边界是当前代码和上述证据。两项历史协议修复已有离线依据；当前两项 P1 仍存在；平铺参数后的模型表现、状态契约修订和真实浏览器体验，仍需后续验证。
