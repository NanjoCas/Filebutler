// Words for the codes the server returns, shared by the chat page (app.js), the task
// editor (tasks.js) and the log page (logs.js). The server sends tool names, stop reasons,
// task states and condition kinds; only this file turns them into words.
const EFFECT_NAMES = {create: '创建目录', move: '移动', rename: '重命名', trash: '移入回收区'};
const TASK_STATES = {running: '待继续', completed: '已完成', partial: '没有完成', needs_review: '请确认结果', waiting_input: '等你回答', interrupted: '已中断', cancelled:'已取消'};
// A read-only task under review is an answer with cited fragments (agent/reporting.display_state).
const taskState = task => task.status === 'needs_review' && !(task.operation_history?.length || task.operations?.length) ? '已回答' : TASK_STATES[task.status] || task.status;
const CONDITION_NAMES = {path_exists:'路径存在', path_absent:'路径不存在', directory_empty:'目录为空', sha256:'内容哈希一致', tool_success:'工具执行成功（仍需核实）', manual_review:'人工判断', evidence_valid:'证据有效', plan_ready:'计划已保存', plan_applied:'计划执行已核验', receipt_verified:'执行回执有效', file_plan_verified:'文件计划位置与内容已核验', files_placed:'按执行前清单归集，原名与内容一致', empty_directories_removed:'范围内已无可见空子目录'};
const TOOL_LABELS = {
  prepare_file_plan: "生成文件操作清单并预检", execute_file_plan: "本地执行并核验文件计划", inspect_file_plan: "查看文件计划和阻塞原因",
  ask_user: "向你确认不清楚的地方", start_task: "登记你的要求与限制", revise_task: "按更正修改任务（需确认）", set_task: "登记目标、范围与约束", finish_task: "逐项核验任务完成条件", inspect_run: "只读查询运行原因", inventory_files: "本地统计文件",
  list_dir: "查看文件夹", search_files: "查找文件", search_content: "检索文件内容", read_file: "阅读文件",
  file_info: "核对文件信息", find_duplicates: "扫描重复文件", apply_dedup: "处理重复文件", deduplicate: "本地查重与清理", report_findings: "核对证据并生成答复",
  paper_profiles: "批量获取论文片段", submit_classification: "保存分类计划并生成表格", apply_classification: "本地批量执行分类计划",
  begin_step: "准备当前任务步骤", make_dir: "创建文件夹", move: "移动文件", rename: "重命名文件", trash: "移入回收区",
  trash_many: "批量移入回收区", finish_turn: "结束本轮",
};
// Labels say what the user does next (agent/reporting.display_state); task states are unchanged.
const DISPLAY_LABELS = {done: "已完成", answered: "已回答", chosen: "已按你的选择处理", confirm: "请确认结果", waiting: "等你回答", unfinished: "没有完成", cancelled: "已取消"};
function outcomeLabel(result) {
  const shown = DISPLAY_LABELS[result?.report?.display];
  if (shown) return shown;
  return ({done: "已完成", answered: "已完成", explained: "已完成", partial: "没有完成", needs_review: "请确认结果", waiting_input: "等你回答", cancelled: "已取消", paused: "已暂停", cost_budget:'没有完成：费用达上限', token_budget:'没有完成：Token 达上限', max_steps:'没有完成：执行轮数达上限', repeated_call:'没有完成：重复调用', repair_limit:'没有完成：连续失败无进展', exception:'运行失败', unverified_answer: "没有完成：未形成可核验结果", unstructured_terminal: "没有完成：回复格式不完整"})[result?.stop_reason] || "已停止";
}
