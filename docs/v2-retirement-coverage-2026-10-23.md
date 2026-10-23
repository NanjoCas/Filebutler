# v2 退役：被删测试的覆盖清单（2026-10-23）

按用户决定（2026-10-23）：v2 协议、旧控制器（LocalRuntime）、自检（reflect）、上下文压缩和旧评测 runner 一起退役，相关测试删除，**只记录、不移植**。删除前的完整代码见本地标签 `archive-v2-legacy-20261023`（基线 699 passed）。

本清单逐条说明：被删测试所测的机制在 v3 下是否还存在；如果还存在，有没有别的测试覆盖。“无 v3 测试”表示机制还在，但目前没有直接测它的用例，这是已知缺口，不是遗漏。

## 删除的测试文件

| 文件 | 测试数 | 主要内容 |
| --- | ---: | --- |
| test_task_runtime_v2.py | 13 | v2 的 set_task 流程 |
| test_policy_redesign.py | 21 | begin_step、旧完成条件、v2 提示 |
| test_phase1_completion.py | 15 | edit_task / apply_task_edit、旧约束谓词、eval.phase1 |
| test_local_dispatch.py | 7 | 旧控制器的本地捷径 |
| test_runtime.py | 11 | LocalRuntime（topic/分类路由） |
| test_reflection.py | 8 | 结束前自检 |
| test_week3_context.py | 8 | 上下文压缩 |
| test_live_regressions_20261021.py | 20 | 10-21 真实运行的 v2 调用回放 |
| test_live_regressions_20261021_02.py | 10 | 同上（第二批） |
| test_week2_eval.py / test_real_data.py / test_regressions.py | 5 / 6 / 1 | 旧评测 runner 与 tasks_*.yaml 的判分 |

`test_week3_context.py` 的两个分页测试和费用预算测试，以及 `test_reflection.py` 的缓存计价测试，和 v2 无关，测的是保留下来的机制，**原样保留**在 `tests/test_budget_and_paging.py`。共享回放工具（Replay/Session/fixtures）迁到 `tests/helpers.py`。

## 一、机制已删除（无需覆盖）

| 机制 | 原测试 |
| --- | --- |
| set_task / edit_task / apply_task_edit / begin_step 及 v2 工具定义 | test_task_runtime_v2、test_phase1_completion（编辑预览/确认/竞态/取消恢复）、test_policy_redesign（step 相关） |
| 单步写工具 move / rename / make_dir / trash、apply_classification | test_live_regressions_20261021（root_only legacy move 等） |
| 旧完成条件 path_exists / path_absent / directory_empty / sha256 / tool_success / files_placed / empty_directories_removed，以及 evaluation=execution_fact | test_policy_redesign（placement、execution_fact、legacy manifest）、test_phase1_completion（空目录、内容保持）、test_result_reporting 中的归集/清理用例、test_p0_binding 的 tool_success 用例、test_live_regressions_20261021 的 dedup+空目录用例 |
| 约束谓词 name_glob / empty_directory / same_extension / content_unchanged | test_phase1_completion（extension_and_content、empty_directory_and_cumulative_count） |
| 关键词路由与本地捷径（LocalRuntime、local_dispatch、topic 预算） | test_runtime、test_local_dispatch |
| 自检 reflect、上下文压缩 ContextManager | test_reflection、test_week3_context |
| 旧评测 runner / replay / phase1、tasks_*.yaml、eval.checker 的 check/select | test_week2_eval、test_real_data、test_regressions、test_phase1_completion 最后一条 |

## 二、机制保留，v3 有测试覆盖

| 机制 | 被删测试 | v3 覆盖 |
| --- | --- | --- |
| 写操作必须先登记任务 | task_runtime_v2 write_cannot_be_authorized_by_model_flag_alone | protocol_v3 v3_plan_needs_a_registered_task、read_only_task_cannot_gain_write_effects_from_a_plan |
| 纯文本收尾不能掩盖未完成部分 | task_runtime_v2 plain_final_cannot_hide_unfinished_subgoal | protocol_v3 unfinished_cannot_turn_a_failed_part_into_a_passed_one、a_blocker_no_one_explained_stays_in_the_answer |
| 任务修订按版本、经确认才生效 | phase1 edit_preview / stale_and_foreign / model_edit_requires_confirmation | protocol_v3 revision_compiles_to_a_reviewed_patch、rejected_revision_keeps_the_task、unstarted_plan_is_rebased_after_a_revision |
| 规则在确认和写入前拒绝 | phase1 rule_rejects_before_move_and_confirmation | protocol_v3 user_limits_reject_before_confirmation_and_any_write、rule_rejection_states_the_rule_meaning |
| 数量上限（含跨轮累计） | policy_redesign operation_limit_has_explicit_time_scope、run_count_does_not_reset…；live_02 max_count 两条 | protocol_v3 max_files_does_not_count_created_folders、count_rejection_states_what_was_counted |
| 文字限制显示在确认卡片并保持复核 | phase1 unknown_text_constraint_is_shown_on_confirmation | protocol_v3 text_limit_is_on_the_confirmation_and_keeps_review |
| 跨轮更正 | live_regressions cross_turn 三条 | protocol_v3 scenario_cross_turn_correction |
| 分类计划已形成/已执行的核验 | live_regressions real_classification 三条、run_outcomes formed_and_applied… | file_plans classification_bridge_compiles_paths_and_preserves_semantic_review |
| 去重回执按部分归属 | live_regressions dedup 用例 | protocol_v3 dedup_part_is_attributed_by_its_receipt_without_begin_step、scenario_dedup_cleanup |
| 去重保留文件被外部移动后不能通过 | live_02 receipt_keeper_moved_outside_the_log_still_fails、task_runtime_v2 receipt_is_revalidated | latency local_post_apply_verification_detects_keeper_change |
| 已解决的阻塞不进主回复 | live_regressions blocker 三条、p0_binding blocker_lifecycle… | protocol_v3 a_reason_the_model_declared_is_stated_once_per_part_and_blockers_move_to_details、a_blocker_no_one_explained_stays_in_the_answer |
| 未执行任何操作的写任务等待用户 | live_02 write_task_that_executed_nothing_waits_for_user | protocol_v3 a_plan_creating_a_target_folder_reminds_the_model_and_waits（waiting_input） |
| 有歧义时保留模型的提问 | live_02 ambiguous_target_keeps_model_question | protocol_v3 ask_user 系列；干跑 holdout_ambiguous_target |
| 无密钥时的提示 | local_dispatch opt_out…lazy_client | run_outcomes missing_model_configuration_has_a_specific_safe_reason |
| 费用/Token 预算中止 | task_runtime_v2 budget_checked_before_first_request… | test_budget_and_paging cost_budget_stops_run（保留） |
| 检索索引随文件内容变化重建 | policy_redesign full_text_search_rebuilds_index… | retrieval cache_refreshes_on_edit（原测试的移动/改名/回收部分用的是单步写工具，已删去） |
| 列表分页、费用预算、缓存计价 | week3 / reflection 中 4 条 | 原样保留在 test_budget_and_paging.py |

## 三、机制保留，但目前没有直接的 v3 测试（已知缺口）

| 机制 | 被删测试 | 说明 |
| --- | --- | --- |
| 受保护子目录阻止移动其父目录 | phase1 protected_descendant_blocks_parent_directory_move | `constraints._members` 仍展开目录成员检查 keep_unchanged；没有 v3 用例直接测“父目录整体移动” |
| 新建多级目录时每一级都检查范围 | phase1 creation_checks_all_missing_ancestors_against_scope | `constraints.creations` 仍在；无 v3 用例 |
| 整项任务的补读上限跨续做保持 | policy_redesign task_read_budget_survives_continuation… | task_read_limit 仍在；只有 test_classification 测了分类读取预算 |
| 来自已回收文件的证据失效 | live_02 evidence_from_file_recycled_outside_this_task_is_invalid | verifier 的 evidence_valid 分支仍检查；无 v3 用例 |
| 旧分类目录不能绕过新任务的读取范围 | task_runtime_v2 old_catalog_cannot_bypass_new_task_read_scope | 无 v3 用例 |
| 真实运行连接失败记为“阻塞”而非质量失败 | phase1 live_runner_marks_connection_failure… | 原测的是已删的 eval.phase1；eval.live_policy 的同类行为无测试 |

| 查询运行结果后，同批写操作不执行 | runtime remaining_calls…、run_outcomes inspection_ends_turn_and_blocks_all_queued_mutations | 两条都用 set_task/make_dir，已删；无 v3 用例 |
| 中止原因（轮数/费用/Token/重复）写入记录并在下一轮可查 | run_outcomes stops_return_actual_reason_and_persist_it_for_next_turn | 用 set_task/make_dir，已删；inspect_run 的读取仍由 legacy_budget_stop_can_be_inspected… 覆盖 |
| 工具意外异常时补齐所有未回复的调用 | run_outcomes unexpected_tool_failure_closes_pending_calls… | 用 set_task，已删；无 v3 用例 |
| 确认等待时间不计入运行耗时 | latency exact_timing_excludes_confirmation | 用单步 trash，已删；无 v3 用例 |
| 撤销遇到占用时跳过、不覆盖 | week2_safety undo_skips_when_occupied | 用单步写工具，已删；文件计划的撤销由 file_plans / plan_ordering 覆盖，但“占用时跳过”没有 v3 用例 |
| 模型写的名字与磁盘名字不同（连字、大小写、NFD）时保留真实名字 | week2_safety move_keeps_real_name…、filename_with_leading_space | 用单步 move，已删；plan_ordering a_slightly_misspelt_source… 覆盖一部分 |
| 网页确认后可预览、撤销 | web_confirmation review_allows_preview_before_approval_and_can_undo_after | 用单步 trash，已删；web_ui 的确认交互两条已改用文件计划触发 |

## 四、混合文件中删除的测试（2026-10-23）

| 文件 | 删除 | 原因 |
| --- | --- | --- |
| test_p0_binding.py | 整个文件（8） | 全部经 set_task 登记任务 |
| test_result_reporting.py | 11，保留 2 | 归集/清理快照条件（files_placed、empty_directories_removed、tool_success）已删 |
| test_run_outcomes.py | 7，保留 7 | 用 set_task / make_dir / path_exists |
| test_file_plans.py | 5，另删 1 个已删谓词的参数 | path_exists / files_placed 条件、set_task、retain_step |
| test_classification.py | 13，保留 3 | 旧执行器 apply_classification 与 LocalRuntime 路由 |
| test_week1.py / test_week2_safety.py | 3 / 7 | 单步写工具 |
| test_repetition.py | 2 | 单步写工具；见下方“发现的缺口” |
| test_retrieval.py / test_web_confirmation.py / test_dedup.py / test_latency.py / test_web_ui.py | 1 / 1 / 1 / 2 / 2 | 单步写工具、无任务直接写入、旧的保留轮次归档方式、set_task |

其余调整只改了与机制无关的部分：无任务时回答的结束状态从 done 变为 answered（运行时始终开启）；模型客户端改为首次请求时创建；测试数据里的条件种类换成 v3 的；两条网页确认测试改用文件计划触发确认。

## 五、清理中发现的缺口（未修，待决定）

- **被拦下的调用不参与重复检测**：v3 运行时里，`prepare` 拦下的调用（如非法 JSON）直接返回错误，不经过“连续 3 次相同调用即中止”的检查。旧测试 `test_repeated_invalid_json_is_also_stopped` 只在已退役的无运行时路径上成立；生产路径（此前 task_runtime 已默认开启）一直是这样。模型反复发同一个非法调用时，只会在轮数上限处停止。阶段 A 要求行为不变，所以只记录。

## 六、旧任务的处理

用户 sandbox 中有 v2 时期登记的任务。`agent/task.py` 的 `is_legacy` 判断：条件不在 6 种之内、使用 execution_fact，或约束谓词不在 within/outside/extensions/max_count 之内，即为旧任务。旧任务：

- 核验时每个条件显示“这是旧版本（协议 v2）登记的任务，只读，不再核验”，不报错；
- 不能用 revise_task 修订，不能续做写入（同一提示）；
- 旧对话历史里的 v2 工具调用（set_task、move 等）在发给模型前折叠成“[旧版本工具记录]”摘要（`agent/history.py`）。
