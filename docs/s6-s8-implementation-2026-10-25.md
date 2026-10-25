# 路由改造 S6–S8 实施记录（2026-10-25）

方案见 `docs/agent-routing-redesign-2026-10-24.md` 第 7、8.2、9、11–13 节。用户要求"做 s6-s8，并测试好"。本文记录做了什么、怎么验证、和方案的偏差，以及还没做的部分。所有测试都用临时目录，没有调用付费模型。

## 1. 结论

| 切片 | 内容 | 状态 | 证据 |
|---|---|---|---|
| S6a | 流程、结果、核验三维状态，判定器 v4 与 v3 并列 | 完成 | `agent/states.py`；`tests/test_states.py` 15 项；重判 778 个历史场景 |
| S6b | 结构化工具结果、16 类错误、恢复动作、局部失败进度 | 完成（按方案的迁移顺序） | `tools/registry.py`；`tests/test_tool_results.py` 17 项 |
| S7 | 检索覆盖数据、覆盖账本、7 个冻结样例 | 完成（有两处偏差，见第 5 节） | `agent/coverage.py`、`eval/coverage_samples.py` 7/7；`tests/test_coverage.py` 6 项 |
| S8 | 真实浏览器验收、迁移和回滚演练 | 完成；真实模型验收待授权 | `eval/browser_acceptance.py` 6/6；第 6 节 |

离线测试 488 → 526 项，全部通过。模型可见提示和工具定义没有变化，敏感检测关时 31,048 字、开时 31,152 字（上限 31,311）。模型看到的工具结果仍然只有 status、text、error_code、retryable 四个字段。

## 2. S6a：三维状态

`agent/states.project()` 是一个纯函数，输入运行已有的事实：结束原因、报告、任务（检查结果、操作、问题）和文件计划。输出：

- `flow_state`：active / waiting_input / ended / interrupted / cancelled
- `result_state`：answered / fulfilled / partial / no_result
- `verification.mechanical`：not_applicable / pending / passed / failed / unknown
- `verification.semantic`：not_required / unreviewed / accepted / rejected
- `next_action`：none / answer_question / confirm_result / recover / retry
- 另有 `parts`（每个部分的旧结果，加两项核验，以及 `skipped_by_user`）、`business`、`registered`、`old_state`、`version`。

使用位置：
- 结果写在 `report['states']`，失败路径也写；同时存进运行记录（`run_outcomes`）。
- `report.state`、`display` 和网页显示都不变，网页不显示这些内部枚举。
- 模型不能提高任何一维，核验结果只来自本地检查。

几个关键判断：
- 有引用片段的只读查找：ended / answered / passed / unreviewed / none，网页仍显示"已回答"。
- 缺证据的查找：partial（mechanical=failed），不会因为是只读就算已回答。
- 用户写的自由文字限制（例如 dedup_cleanup 里的 notes）计入 semantic=unreviewed。初版漏了这一条，重判时发现 v4 会放过 dedup_cleanup，已修复；这正是 S5 审计说"不能放行"的那一类。
- 按选择什么都不做（`report.result=by_choice`）：result=fulfilled，不论有没有登记任务，结论都一样（S5 审计 4.3）。
- 文件计划里有执行中断、结果不明的步骤：mechanical=unknown，next_action=recover。

## 3. 判定器 v4（与 v3 并列）

`eval/judge.py` 的 `JUDGE_VERSION='v3+v4.1'`。每个回合除了 v3 的 `status` 维度，还会输出：
- `status_v4`：不计入 v3 的通过与否；
- `accepted_v4`：文件、回答、约束三维沿用 v3，状态维度换成 `status_v4`。

退出码仍然按 v3 计算。期望由冻结的 v3 状态列表推出，映射表 `V4_FROM_V3` 在重判历史结果之前就已冻结：

| v3 期望 | v4 接受的状态 |
|---|---|
| done | ended，fulfilled，机械核验 passed 或 not_applicable，next_action=none；或 ended + answered + 已登记任务；或解释运行记录（explained） |
| answered | ended + answered（机械核验 not_applicable 或 passed）；或 ended + fulfilled + business=by_choice |
| needs_review | ended，fulfilled 或 answered，semantic=unreviewed |
| waiting_input | waiting_input，next_action=answer_question |
| partial | ended 或 interrupted，partial |
| cancelled | cancelled |

几点说明：
- 改了文件、还在等用户确认结果的任务（confirm_result）仍然不算 done。
- 没有登记任务的普通回复，仍然不算"完成了一个任务"。
- 场景如果声明了 `business`，v4 同样检查。

重判 `eval/results/policy_live_*` 下全部 156 个批次、778 个场景次数（脚本 `docs/repro/s6-v4-rejudge-2026-10-25.py`；老批次的期望取自同 ID 的冻结场景）：

- v3 通过 675，v4 通过 680。
- 没有任何场景 v3 通过而 v4 不通过。
- 5 处不一致，都是方案第 7 节有意改变的两类：
  - 有引用的只读回答，4 次：`off_find_marker` ×2（第 37 轮）、`target_conflict`（第 38b 轮）、`ocr_sideways_photo`（第 39 轮）。v3 因为 needs_review 判不过；v4 认为它们已回答，答案正确与否由回答维度单独检查（这几次回答维度都通过）。
  - 按选择不做，1 次：第 44 轮 r2 的 `ans_do_nothing`，report.result=by_choice。第 40、41 轮的 `ans_do_nothing` 发生在 by_choice 契约实施之前，报告里没有记录选择，v4 仍判不过。

## 4. S6b：结构化工具结果

`ToolResult` 新增以下字段，全部只留在本地（trace、observations、`runtime.results`）：
- `result_id`：运行 ID 加调用 ID，同一个 ID 只记录一次；
- `data`；
- `references`：本次调用新登记的证据、回执、分类方案、目录；
- `operation_refs`：本次写入的日志行；
- `progress`：completed / remaining / unknown；
- `error_class`、`recovery`、`display_ref`。

16 类错误按方案 8.2 定义：
- 先看具体错误码，例如 AMBIGUOUS_GOAL、USER_DECLINED、CONFIRMATION_TIMEOUT；
- 再看本地报错的固定措辞，例如"任务版本已改变""已达到任务检索预算""超出任务读取范围"；
- 没写明原因的意外异常归为 INTERNAL_ERROR；
- 其他被拒的请求归为 INVALID_ARGUMENTS。

原有的具体错误码保留在 error_code 里，无进展保护仍然按 error_code 计数。超时、中断、拒绝时 `retryable=false`、recovery=none，不授权自动重试（方案 8.1）。

迁移顺序和现状：
1. **report_findings**：结果表格放在 `data.display` 里返回，工具不再写 `runtime.final_answer`；引用的证据放在 `data.cited`。
2. **读取和检索**：`references` 由注册层在产生对象的那次调用里记录。旧的"整体比对 artifact 增量"仍作为影子检查运行，两者不一致时记入 `runtime.reference_shadow`，并在 stats 里计数。离线测试中的一次查找没有出现不一致；真实模型运行之后，再决定是否删掉旧的比对。
3. **去重回执与部分失败**：批量回收中途失败时返回 PARTIAL_EXECUTION，progress 写明已回收和剩余的文件，recovery=reconcile。
4. **其他工具**：文件计划停在中途时，progress 区分已完成、剩余和执行中断的步骤。有中断步骤时为 OUTCOME_UNKNOWN（reconcile），下一次执行时由现有的恢复逻辑核对，不会盲目重放。

其余工具统一在循环里补上 result_id 和错误分类。

## 5. S7：检索覆盖

`search_content` 的结果数据（`data.coverage`）包含：
- scan_id、scope、snapshot_id（可读文件和版本的摘要）、index_revision；
- enumeration_complete；
- 已索引、失败、无文字、不支持的文件数；
- pending_ocr_units、ocr_failed_units；
- extract_complete；
- matched_files、returned_files、candidate_truncated；
- query_terms、strategy；
- 每个文件的版本和状态，以及候选列表。

计数方式：
- 待识别和识别失败的页按每个文件的索引状态汇总，不只看本次调用的增量。因此上一次检索留下的扫描页，下一次仍然算待识别。这里修正了原来只按单次增量统计的问题。
- 给模型的文字保持原有说法，只在有覆盖缺口时加一行"覆盖缺口：…不能据此说范围内没有其他相关文件"。

覆盖账本（`agent/coverage.py`）：
- 按请求、范围、枚举快照、索引版本分开记录。
- 每个文件记为三种状态之一：
  - reviewed：读过正文，或在 report_findings 里引用过；
  - excluded：写明原因，例如格式不支持、读取失败、没有文字、扫描页待识别；
  - unreviewed：其余可读但没看过的文件。
- 资料库变化后生成新快照：内容没变的文件沿用之前的核对结果，并注明沿用自哪个快照；新增和改动的文件重新记为未核对。
- 有查找部分的任务，报告详情里有"检索覆盖"表。本地已知存在缺口时（候选被截断、扫描页未识别、有文件夹无法列出、资料库变化），主回复会加一句"已核对 X 个，其余未核对……不代表已经找全"。

7 个冻结样例（`eval/coverage_samples.py`，离线运行，结果在 `eval/results/coverage_samples_20261025/`）7/7 成立：

| 样例 | 结果 |
|---|---|
| CN→EN | 中文原查询命中 0；翻译后的离线代理查询命中 2。模型自己是否会去扩展查询，要真实运行才能测 |
| CN→CN | 不翻译也能命中 |
| 无词项且完整 | 命中 0，抽取完整，没有声称"不存在" |
| 无词项且 OCR 未完 | 3 个扫描页记为待识别，抽取不完整，回复写明 |
| 后几页才相关 | 第 4 页的细节命中 page=4 |
| 超过 top_k | 重合 30、返回 5、核对 5/30，候选截断写明 |
| 扫描间库改变 | 10/10 → 9/11：沿用 9 个，新增和改动的 2 个未核对 |

和方案的偏差：
1. **没有加批量查询参数**。方案要求新增字段必须靠删减说明来抵消字数；现在余量只有 263 字（敏感检测开时 159 字），按方案的退路，继续用多次 search_content。
2. **没有加"找全"标记字段**。原因同上，start_task 和 revise_task 两份 schema 各加一次，敏感检测开时会超过上限。改为每个查找部分都维护账本：只在本地记录，不额外读文件，也不给模型列名单；主回复只在本地确知有缺口时说明。

## 6. S8：浏览器验收与迁移、回滚

### 6.1 真实浏览器

`eval/browser_acceptance.py` 用本机的 Google Chrome 156 通过 DevTools 协议驱动页面：
- 点击是在元素位置发出真实的鼠标按下和抬起，文字用键盘输入；
- 刷新和断网也由浏览器本身完成；
- 模型用一个本地的脚本化 OpenAI 兼容服务代替，不花钱；
- 网页服务通过新增的 `FILEBUTLER_HOME` 改用临时的配置、状态、日志和资料库，没有碰真实的 `sandbox/` 和 `.filebutler-ui/`。

结果 6/6（`eval/results/browser_acceptance_20261025b/`，含截图）：

| 场景 | 检查项 |
|---|---|
| 问题卡按钮与"按选择处理" | 状态"等你回答"；点 A 发送精确选项；回复"已按你的选择保持原样"；旧按钮已禁用；文件没动；回答只调用了一次模型 |
| 确认卡片等待时点"停止" | 卡片关闭；回复"已取消"；文件夹没有建 |
| 允许后撤销 | 允许后文件夹建好；点"撤销文件操作"后文件夹删除；提示"撤销结果（已全部还原）" |
| 运行中刷新页面 | 刷新后仍显示"正在处理"；用户消息和回答各一条；模型只被调用一次 |
| 断网发送后重试 | 断网时提示"消息没有发出"，输入框里的文字恢复；联网后重发：同一条消息，只运行一次，待发记录已清除 |
| 模型服务失败一次后重试 | 本地重试一次成功，只有一条用户消息 |

验收中发现并修复了两处页面问题（第一次运行在 `browser_acceptance_20261025/`）：
- **断网发送失败**：原来显示英文 "Failed to fetch"，并且说"本轮处理已中断"，实际上消息根本没有发出。现在改为提示"网络连接失败，消息没有发出。恢复连接后点发送，会作为同一条消息重发，不会重复执行"，并且不再显示中断卡片。
- **撤销结果标题**：原来不论有没有跳过的项，标题都是"撤销结果（跳过项未还原）"。现在按实际情况显示"已全部还原"，或"N 项未能还原，见下"。

### 6.2 迁移与回滚演练（真实库副本）

S6–S8 没有改数据库结构：新字段都写在已有的 JSON 里（任务的 coverage、观察记录的 result_id 和 error_class、运行记录的 states）。两项演练：

1. **新代码读旧数据**（`docs/repro/s8-new-reads-old-2026-10-25.py`）：复制 `sandbox/.oplog/tasks.sqlite3`，原库不动。新代码逐条读取 34 个任务版本和 73 条运行记录，投影三维状态并重建报告，0 错误，用时 29ms。投影分布：
   - ended/fulfilled 17，ended/partial 9，ended/answered 4；
   - interrupted/partial 1，interrupted/no_result 1；
   - waiting_input/partial 1，waiting_input/no_result 1。
2. **回滚**（`docs/repro/s8-rollback-write-new-2026-10-25.py`、`s8-rollback-old-reads-2026-10-25.py`）：先用新代码在临时资料库里写入一个查找任务（含覆盖账本和结构化观察记录）和一个整理任务；再用推送前的代码（`e3c7d7f`，用 git archive 导出到 scratchpad）读同一个库。两个任务的状态重算结果与保存的一致，运行记录可读，旧代码还能在这个库里继续运行新请求。

## 7. 没做的和限制

- **真实模型验收没跑**，需要单独授权费用。方案第 13 节要求：先跑现有套件，再跑新场景，关键 8–12 组至少各 3 遍。重点要看：
  - `reference_shadow` 不一致的次数；
  - 有覆盖缺口那一句之后，查找类回复的表现；
  - CN→EN 时模型会不会自己扩展查询。
- **v4 只并列报告，没有替代 v3 的退出码**。是否切换，等真实运行积累一段时间后由用户决定。
- 方案第 13 节的可观测事件，这次只加了 result_id、error_class、错误分类计数和影子不一致计数。缓存、问题过期率等其他统计没有新增。
- 浏览器验收用的是脚本化模型，测的是页面、服务和本地运行，不测模型怎么选择。
