# FileButler 项目交接（HANDOFF）

更新时间：2026-10-21（Asia/Shanghai）。供新对话或下一位维护者直接接续。

## 1. 接手先看这几句话

**当前不是“优化已经验收通过”，而是“核心机制已实现，离线测试通过，真实模型复验仍未通过”。**

- 最新离线结果：434 passed，7 项既有依赖弃用警告，38.61 秒。
- 最新真实模型：DeepSeek `deepseek-flash`，8 组/10 轮，78 次请求，估算 ¥0.38568，净运行 176.94 秒。
- 文件结果、内容保护、撤销均为 8/8；但 3 轮正确处理文件后误报 `partial`，还有旧错误继续被显示成当前阻塞。
- 相同回归的上一轮为 75 次请求、168.43 秒、¥0.35239。本轮没有证明总体提速或降低费用。
- 真实复验期间冻结产品代码；复验发现的问题**尚未修复**。最后一次工作只写报告，没有基于失败热修或重跑挑结果。
- 用户最后要求写本交接文档，然后关闭对话。不要把“写交接”当作已授权立即继续改代码、提交、推送或再次调用付费模型。

下一轮最重要的方向：**把机器完成条件、引用绑定和执行编排真正收回本地，减少模型先填旧任务协议、再调用新计划工具的双重负担。不要再针对某个中文词或某篇论文做专项补丁。**

## 2. 项目位置与 Git 状态

| 项目 | 当前值 |
| --- | --- |
| 仓库根目录 | `/Users/nanjo/Desktop/projects/DocAgent/filebutler` |
| 当前对话工作目录 | `/Users/nanjo/Documents/Codex/2026-10-19/new-chat`，不是代码仓库 |
| 当前分支 | `master` |
| 当前 HEAD | `a489b21`：新增本地 Web 界面、论文检索与分类计划批量执行 |
| GitHub | https://github.com/NanjoCas/Filebutler |
| origin fetch | `https://github.com/NanjoCas/Filebutler.git` |
| origin push | `git@github.com:NanjoCas/Filebutler.git` |

**有大量未提交修改及未跟踪的核心文件。** 当前代码不能仅靠远程仓库或 HEAD 还原；交接时没有自动 commit/push。本轮新增及此前多轮的任务系统、计划执行、测试、设计文档很多仍显示为 `??`，并不代表它们可以删除。

接手先运行 `git status --short` 和 `git diff`，保留已有工作，不做 reset/clean、整文件回滚或顺手覆盖。日志与真实语料等被 `.gitignore` 排除，包括 `eval/results/`、`logs/`、`data/real/`、`sandbox/`、`.filebutler-ui/`、`.env`。只提交代码不会包含原始验收证据或会话数据，需要时另外保存；本交接不要求现在提交它们。

本机 Git 不支持 `git branch --show-current`，使用 `git rev-parse --abbrev-ref HEAD`。此前未找到适用的 `AGENTS.md`；新环境仍应重新检查。

文件系统授权与网络权限按新会话实际环境处理：本次项目目录曾需显式请求写权限，不应假定它永远在新会话默认可写范围内。

## 3. 用户目标与长期偏好

产品是本地文件/论文管理 Agent：自然语言理解需求，检索、查看、分类、去重、移动、改名、回收及撤销；配套 FastAPI 本地网页，多对话和文件预览。

用户反复强调：

1. 从错误中发现架构弊端，优先解决泛化和职责分工，不为某次目录、主题或中文表达写死路由。
2. 减少模型请求、重复上下文和纠错，把枚举、哈希、表格、预检、确定步骤、验收等放本地。
3. 模型负责需求含义、分类标准、对象选择和歧义；不能靠放宽权限或伪造完成条件提高通过率。
4. 失败或中止直接说明具体原因，避免长终端日志充当答复。必要细节可展开；表格和文件引用可点击预览。
5. 用户确认前应能看见内容和来源/目标。重复文件同组对比，待回收项浅红色；不能把“保留”和“删除”整体分成两组。
6. 检索先用 BM25，测出语义召回短板再评估向量检索。当前主要耗时是模型编排，不应直接把向量数据库当作速度问题的答案。
7. 区分代码实现、离线测试、真实模型、浏览器/人工验收。不能只看工具成功或文件位置正确，就宣称整体完成。

用户曾明确授权真实模型验收，最近一次已经完成。当前没有待续跑的验收进程。本交接不指示新会话自动重跑收费测试。

## 4. 优先阅读的材料

按这个顺序读；历史方案与最新结果矛盾时，以最新复验事实为准：

1. [2026-10-21 真实模型复验](/Users/nanjo/Desktop/projects/DocAgent/filebutler/docs/live-acceptance-2026-10-21.md)：最新失败、逐轮数据和后续优先级。
2. [2026-10-21 本地计划执行优化 V1](/Users/nanjo/Desktop/projects/DocAgent/filebutler/docs/local-execution-optimization-2026-10-21.md)：实际实现、参考案例和限制。
3. [2026-10-20 策略重构](/Users/nanjo/Desktop/projects/DocAgent/filebutler/docs/policy-redesign-2026-10-20.md)。
4. [Agent 架构 V2](/Users/nanjo/Desktop/projects/DocAgent/filebutler/docs/agent-architecture-v2.md)。
5. [工程笔记](/Users/nanjo/Desktop/projects/DocAgent/filebutler/docs/engineering-notes.md)、[实验记录](/Users/nanjo/Desktop/projects/DocAgent/filebutler/docs/experiments.md)：末尾记录最新交付与踩坑。
6. [2026-10-20 真实模型验收](/Users/nanjo/Desktop/projects/DocAgent/filebutler/docs/live-acceptance-2026-10-20.md)：上一轮基线和已知评测缺陷。
7. [README](/Users/nanjo/Desktop/projects/DocAgent/filebutler/README.md)：启动与功能概览；部分旧章节仍含历史 V7 描述，当前默认以 config 和运行时代码为准。

## 5. 启动、配置和数据

在仓库根目录使用现有 `.venv`；无需为了接手重新安装依赖。

```bash
cd /Users/nanjo/Desktop/projects/DocAgent/filebutler
source .venv/bin/activate
python -m pytest -q
uvicorn webapp:app --host 127.0.0.1 --port 8000
```

网页：<http://127.0.0.1:8000>。CLI 示例：`python main.py run "用户指令"`；撤销：`python main.py undo`。CLI 和 Web 都可能操作配置的真实 sandbox，测试必须传临时沙盒，不能直接用这些示例去整理用户原件。

实际模型由环境变量 `DEEPSEEK_API_KEY` 提供凭据；文档/日志只记录变量名，不能打印或写入密钥。最近真实测试时变量可用，不代表新终端一定继承。项目配置的 API 地址为 `https://api.deepseek.com`。

当前关键配置：

| 配置 | 值 |
| --- | --- |
| model / prompt | `deepseek-flash` / `v8_tasks` |
| local_runtime / task_runtime | `true` / `true` |
| local_dispatch | `true`，仅旧控制器有效；默认 V2 不走关键词捷径 |
| max_steps | 25 |
| task_query_limit / task_read_limit | 12 / 40，跨续做累计 |
| task_repair_limit | 6；0 关闭无进展修复保护 |
| repeat_detection / retrieval | `true` / `true` |
| llm_timeout / llm_retries | 60 秒 / 1；真实验收覆盖为 0 次重试 |
| cost_budget | 每轮 ¥0.30 软预算，非硬账单上限 |
| confirm | `risky`；V2 强制展示写入确认，调用方显式 `never` 除外 |
| context_compress / reflect | `false` / `false` |
| sandbox | 仓库下 `./sandbox` |

数据位置：

- `.filebutler-ui/state.sqlite3`：网页会话及历史。
- 各资料库 `.oplog/tasks.sqlite3`：任务修订、编辑、运行结果和新文件计划；按 root/session 隔离。
- 各资料库 `.oplog/<run_id>.jsonl`：可逆操作日志；撤销后改为 `.undone.jsonl`。
- 各资料库 `.oplog/document-cache.sqlite3`：跨对话 PDF 解析缓存。
- 各资料库 `.trash/`：回收对象。
- `logs/`、`eval/results/`：运行与评测记录；都在忽略目录内。

## 6. 当前架构与代码入口

下表代码路径均相对于仓库根目录。

| 文件/模块 | 作用与重要边界 |
| --- | --- |
| `agent/loop.py` | 模型循环、工具分发、预算、事件与收尾；默认接入 TaskRuntime。 |
| `agent/task.py` | Pydantic 任务/条件/规则、TaskStore SQLite、修订/CAS、任务编辑与人工复核；完成条件仍暴露给模型，是最新问题来源。 |
| `agent/task_runtime.py` | set_task、begin_step、finish_task、编辑/查询、新计划控制工具；权限和约束检查；控制工具与普通工具的观察处理不同。 |
| `agent/file_plans.py` | prepare_file_plan / execute_file_plan / inspect_file_plan。模型选择对象，本地生成目录、范围需求、清单、源快照、摘要、执行与核验；已有任务不自动扩权限。 |
| `agent/completion.py` | files_placed / empty_directories_removed 的执行前快照及核验；无效快照前置拒绝，旧任务写入前也检查。 |
| `agent/constraints.py` | 本地操作谓词、祖先/后代范围、数量/内容等检查；不按自然语言关键词授权。 |
| `agent/policy.py` | 用户偏好、证据/输出策略、目标绑定、依赖敏感的 retain_step。 |
| `agent/verifier.py` | 当前状态、执行事实、证据、分类与通用计划回执核验；证据有效不等于语义正确。 |
| `agent/reporting.py` | 正文摘要与折叠详情；当前 blocker 生命周期有缺陷。 |
| `agent/outcomes.py` | 费用、步数、重复/无进展、网络/配置失败等本地停止报告。 |
| `agent/repetition.py` | 相同连续调用保护 + 按机器错误类/执行及证据进展计数的 NoProgressGuard。 |
| `agent/artifacts.py` | F/E/P/R 等文件、证据、分类计划、回执身份及版本；网页按会话持久化，不是完整全库共享文档目录。 |
| `agent/document_cache.py` | SHA256+页范围+解析器版本的 PDF 文本缓存；错误不缓存，缓存不可用回退解析。 |
| `agent/history.py` | 旧工具历史归档/投影；完整 UI 历史保留，还不是完全按任务相关性检索上下文。 |
| `tools/registry.py`、`tools/scopes.py` | 能力声明、Typed ToolResult、作用域与操作预览适配；保留旧字符串返回兼容。 |
| `tools/classification.py` | 论文卡片、完整分类提案、旧本地执行器；分类由模型选择，可传维度/目录/编号。 |
| `tools/dedup.py` | 字节、规范化全文及近似候选去重；近似版本需逐组核对，不能用相似度自动当重复。 |
| `tools/retrieval.py`、`tools/fs_read.py` | BM25、文本/PDF 读取；页范围可配置，默认前两页，不保证穷尽，不含 OCR。 |
| `tools/fs_write.py` | 旧单项创建/移动/改名/回收，继续保留。 |
| `safety/atomic.py` | 新执行器的原子禁止覆盖移动：macOS renamex_np、Linux renameat2；缺接口拒绝，不退回覆盖型 rename。 |
| `safety/oplog.py`、`safety/confirm.py` | 写入日志/undo、确认策略；日志逐项 flush/fsync，新计划带 action_id。 |
| `webapp.py`、`web_confirm.py`、`web/` | FastAPI、结构化确认预览、多会话、文件预览、Markdown、事件动画及任务复核。 |

### 新计划链路

- `prepare_file_plan` 接受 description + actions 或 selection；分类可只给精确 `classification_plan_id`。
- selection 在本地枚举源文件；actions 指定 create/move/rename/trash 对象。目标为具体相对路径，支持当前会话有效 F 文件引用。
- 新任务由本地生成权限需求和 `file_plan_verified(reference=精确X标识)`；已有任务保留原约束，复合任务必须明确 goal_ids。
- `execute_file_plan` 只接受精确 X ID，先预检、一次确认、确认后重查、本地批量执行和同清单核验，不接受 latest。
- SQLite 保存计划、执行前意图和逐项进度；完整日志与对象身份/哈希一致才恢复已完成动作。结果不明的崩溃窗口停止核查，不自动重放。
- 已完成计划重复执行只核验、不重复修改。部分续做保留前轮账本和日志；撤销依然按各 run 进行。

限制：新执行器的资料库锁尚未覆盖全部旧写入路径，并且确认期间持锁；不等于全应用统一事务。非空目录移动、覆盖、互换/循环依赖暂需拆成明确阶段。PDF 缓存不是完整文档库、OCR 或向量索引。

## 7. 最近一次真实验收：必须保留的事实

原始结果目录：

`/Users/nanjo/Desktop/projects/DocAgent/filebutler/eval/results/policy_live_20261021_01/`

重要文件：

- `manifest.json`：配置、输入、金标准、原论文与产品代码哈希。
- `report.json`：原始评测文件结果，保留原口径，**不能只看 passed_local=8 或进程退出码 0**。
- `comparison.json`：旧/新逐轮数据、工具序列、失败条件、主答复。
- `review.json`：Codex 事后复核，`overall_accepted=false`；不是独立人工金标准。
- 场景子目录：各轮 `history.json`（不含 reasoning_content）、`trace.jsonl`、`events.json`、`approvals.json`。

这些文件在忽略目录里，本机存在，不能期待从 GitHub 自动获得。用于生成 comparison/review 的临时辅助脚本不属于正式仓库入口；下一位应直接读这些 JSON，必要时再建立可维护的汇总工具。

相同上一轮结果：`eval/results/policy_live_20261020_01/report.json`。当天总计 112 次 API、约 ¥0.62 包含其他探针；**与本轮可直接对照的 Agent 套件只有 75 次、约 ¥0.35239**，不要混用。

| 场景 | 模型请求：旧→新 | 最新状态与结论 |
| --- | ---: | --- |
| 根目录统计并移动 | 4→10 | 文件正确，误报 partial。 |
| 去重并清空目录 | 6→18 | 最终 done，但大量协议纠错。 |
| 六篇真实论文分类方案 | 7→6 | needs_review；6/6 对齐既有小样本金标准，未执行移动。 |
| 执行上述分类计划 | 12→7 | 文件正确，误报 partial。 |
| 按应用领域整理三份 PDF | 25→12 | 上次中止，本轮文件正确、needs_review。 |
| 跨轮更正：首轮移动 | 6→8 | 文件正确，误报 partial。 |
| 跨轮更正：移回 b | 5→6 | 更新同一任务，done。 |
| 只讨论不执行 | 1→1 | answered，无扫描/写入，答复仍长。 |
| 无匹配结果检索 | 7→7 | needs_review，无伪造正例，仍输出长否定表格。 |
| 目标同名冲突 | 2→3 | answered，无改动，正文正确说明冲突。 |

累计模型 Token：1,413,297→1,446,359；工具调用：117→106。工具数下降没有转化成模型往返下降。模型请求 175.24 秒，占本轮净耗时 99.04%；本地工具约 1.316 秒。

费用使用项目配置费率估算，不是账单核对。该套已用于调试，是回归，不是新盲测；跨天服务延迟/缓存/采样不可控，不能把单次时间差当稳定因果结论。自动确认只验证临时数据执行，不代表人工确认或浏览器验收。

## 8. 未解决问题与建议接手顺序

### P0：收回机器完成条件和引用绑定，消除错误 partial

三次具体复现：

1. `root_only`：最终答复正确报告 2 个 txt，但模型单独建立 report_count 目标，list_dir 观察没有绑定它；tool_success 检查失败。
2. `real_classification` 第二轮：模型自行登记 file_plan_verified，却留空 reference；执行器回执在具体 X ID 下，核验查空 ID 失败。files_placed 已通过。
3. `cross_turn_correction` 第一轮：普通移动错误使用分类专用 plan_applied，找不到分类引用；实际 a/b 文件都已正确移动。

代码先看 `agent/task.py` CompletionCondition、`agent/task_runtime.py` schemas/dispatch/after、`agent/file_plans.py` prepare/execute、`agent/verifier.py`。机器字段不能继续由模型随意编译。预检还需检查“该条件适合该操作、引用能确定绑定”，不只检查枚举或已有快照。执行和验收共用本地计划，同时保持已有用户要求，不能通过删掉目标或弱化条件“修好”状态。

### P0：组合任务按具体动作绑定约束

`dedup_cleanup` 将去重与清空目录目标同时绑定，导致 empty_directories_removed 的保护把去重文件也当作“只能回收空目录”，反复拦截 deduplicate/apply_dedup/通用计划/trash。后来任务编辑和拆步才成功。

先看 `agent/completion.py` enforce_cleanup_condition、`agent/constraints.py` validate_operations、`agent/policy.py` active_goals、`agent/file_plans.py` _validate。不能把一个全局 active_step 当多种动作的通用语义标签；计划动作应带本地确定的目标/约束归属，且不丢失全局禁止。

### P1：统一 blocker 生命周期

`TaskRuntime` 记录的是带 tool 的 blocker；成功文件计划只清同 plan_id 的 blocker。于是 application_dimension 已完成本地检查，仅待语义复核，正文仍显示已修复的源写范围错误。

先看 `agent/task_runtime.py` dispatch 异常分支、`agent/file_plans.py` execute 收尾、`agent/reporting.py`。需要统一来源/对象/版本/解决状态；未解决原因进主答复，已恢复错误进过程详情，不能无条件删除全部错误掩盖真实失败。

### P1：简化默认工具协议，避免旧表单与新计划叠加

真实模型已经使用新执行器，但通常先 set_task 填底层规则，再登记步骤，再准备计划。当前新增工具扩大了选择空间，没有取代旧复杂入口。非法 delete、create+same_extension、遗漏源写范围等仍消耗多轮纠错。

建议先明确一个模型可见的“意图/对象提案”入口，本地编译机器契约与执行计划；内部控制/兼容工具不必全部同等暴露。要先设计迁移与授权边界，不能简单删除所有旧工具导致复合/跨轮任务退化，也不能新增关键词路由。

NoProgressGuard 仍可漏过跨工具/错误类、伴随证据变化的长修复链。应结合具体失败语义、计划动作与实际进展改进，不以任意缩小 max_steps 或扩大权限替代修复。

### P1：完善验收判定，再做新盲测

现有 `eval/live_policy.py` 原始 passed_local 主要是物理文件断言，错 partial 仍可能退出 0。下一轮应同时断言文件结果、任务状态、主要答复和约束；语义 needs_review 与真正未完成必须分开。冻结原报告，不追改历史分数。增加不参与调试的新组合场景后再做真实模型复验。

### P2：其他未完成方向

- 全应用跨会话/进程写锁、完整崩溃恢复、服务重启后持久任务恢复。
- 独立文档库/位置同步、OCR、持久全文索引；向量检索需要召回实验支撑，当前不优先。
- 更完整的任务相关上下文投影，减少历史机器协议带来的 Token。
- 无结果/纯讨论时的简短答复；正文与详情一致性。
- 浏览器现场、真人确认流程验收；本次只做离线 HTTP/逻辑测试及真实模型临时数据测试。

## 9. 验证与复现

现有测试重点：

- `tests/test_file_plans.py`：真实临时文件、选择集、一次确认、重复执行、undo、冲突/符号链接、权限边界、分类桥接、部分续做及日志后中断恢复。
- `tests/test_document_cache.py`：跨会话/移动复用、内容/页范围变化、解析失败与缓存回退。
- `tests/test_result_reporting.py`、`tests/test_task_runtime_v2.py`、`tests/test_phase1_completion.py`、`tests/test_policy_redesign.py`：任务/规则/核验/报告。
- `tests/test_live_policy_acceptance.py`：旧验收判定器的故障注入；不能替代完整的任务状态验收。
- `tests/test_web_ui.py`：HTTP、预览和确认相关；不等于现场浏览器体验验收。

```bash
cd /Users/nanjo/Desktop/projects/DocAgent/filebutler
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest tests/test_file_plans.py tests/test_document_cache.py tests/test_result_reporting.py -q
/opt/homebrew/bin/node --check web/app.js
/opt/homebrew/bin/node --check web/tasks.js
git diff --check
```

需要真实复验时，正式入口为下方命令。它会收费，输出目录必须是新的；不要覆盖已有原始结果，也不要把示例命令当作要求现在执行。

```bash
.venv/bin/python -m eval.live_policy --live \
  --out eval/results/policy_live_NEXT_UNIQUE_ID --total-cap 1.2
```

评测总 cap 是按轮/请求间检查的软上限。测试过程中冻结产品代码，保存配置与哈希；测试完区分物理断言和总验收。原始结果目录被忽略，不自动提交。

接手后建议先补真实失败对应的回归测试，再修改产品实现。离线脚本能安排“两轮完成”只证明本地能力存在，不能证明真实模型会选择此路径；不要再次用该结果声称实际提速。

## 10. 安全与实现边界

- 不动用户真实文献、sandbox 和会话库做破坏性验收；使用临时数据及原件副本并检查哈希和 undo。
- 权限需求推导不等于已有授权。已有任务的限制只能通过具体可预览、可确认的变更修改；不把共同父目录或根目录自动视为写权限。
- 用户确认绑定具体计划及版本；确认期间对象变更要拒绝/重新预检，不能复用旧确认。
- 语义分类正确性不能由文件哈希或工具成功证明；语义复核仍需保留。
- 不重放不明写入，不以同名/同内容目标存在推断曾成功执行；文件系统和 SQLite 并非统一原子事务。
- 新缓存只共享解析结果，不共享会话授权/证据身份。默认前两页不能被说成整篇或全库穷尽。
- 不打印凭据，不记录模型隐藏推理，不把付费估算当账单事实。

## 11. 新对话可直接使用的接手提示

> 请先阅读 `/Users/nanjo/Desktop/projects/DocAgent/filebutler/HANDOFF.md` 和最新真实模型验收报告，检查未提交改动。当前核心机制已实现、434 项离线测试通过，但真实模型整体验收未通过；先处理机器完成条件/引用绑定导致的三次错误 partial、组合任务动作归属，以及旧 blocker 生命周期，再简化模型可见协议。不要为单个关键词或案例打补丁，不扩大权限，不弱化验收，不覆盖已有日志或未提交代码。继续前先核对我在新对话中的具体要求。
