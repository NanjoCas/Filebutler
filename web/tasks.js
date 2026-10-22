// Structured task edits are previewed and applied explicitly; no model call here.
const EFFECT_NAMES = {create: '创建目录', move: '移动', rename: '重命名', trash: '移入回收区'};
const TASK_STATES = {running: '待继续', completed: '已完成', partial: '部分完成', needs_review: '待复核', waiting_input: '等待补充', interrupted: '已中断', cancelled:'已取消'};
const FIELD_NAMES = {goals: '目标与完成条件', constraints: '约束', read_roots: '读取范围', write_roots: '写入范围', allowed_effects: '允许操作', unresolved_questions: '待确认问题', cancelled:'取消状态', policy:'任务策略'};
const CONDITION_NAMES = {path_exists:'路径存在', path_absent:'路径不存在', directory_empty:'目录为空', sha256:'内容哈希一致', tool_success:'工具执行成功（仍需核实）', manual_review:'人工判断', evidence_valid:'证据有效', plan_ready:'计划已保存', plan_applied:'计划执行已核验', receipt_verified:'执行回执有效', file_plan_verified:'文件计划位置与内容已核验', files_placed:'按执行前清单归集，原名与内容一致', empty_directories_removed:'范围内已无可见空子目录'};
function conditionValue(c) {
  return (CONDITION_NAMES[c.kind]||c.kind)+(c.path?' '+c.path:'')+(c.destination?' → '+c.destination:'')
    +(c.extensions?.length?'；后缀：'+c.extensions.join('、'):'')+(c.exclude_paths?.length?'；排除：'+c.exclude_paths.join('、'):'')
    +(c.evaluation==='execution_fact'?'；仅证明历史执行，不声明当前位置':'')+(c.placement==='preserve_tree'?'；保留相对层级':'')+(c.placement==='mapping'?'；按指定路径放置'+(devMode?'：'+JSON.stringify(c.placements):''):'')+(c.recursive===false?'；仅当前层':'')
    // Tool names and plan/receipt IDs are internal; developer mode shows them.
    +(devMode&&c.tool?' '+c.tool:'')+(devMode&&c.reference?' '+c.reference:'');
}
let taskViewEpoch = 0;
function taskField(label, value = '') {
  const wrap = node('div', 'task-field'), title=node('label','',label), input = node('textarea');
  input.id='task-field-'+crypto.randomUUID(); title.htmlFor=input.id;
  input.value = value; input.rows = 2; wrap.append(title,input); return {wrap, input};
}
function taskValue(field, value) {
  if (field === 'cancelled') return value ? '已取消' : '可继续';
  if (field === 'goals') return value.map(g => g.description + (g.semantic_review ? '（需要复核）' : '')+'\n完成条件：'+g.conditions.map(conditionValue).join('、')).join('\n');
  if (field === 'constraints') return value.map(c => c.description+'\n禁止操作：'+(c.forbidden_effects?.map(e => EFFECT_NAMES[e] || e).join('、')||'无')
    +(devMode?'\n适用目标：'+(c.goal_ids?.join('、')||'全部目标')+'\n本地检查：'+JSON.stringify(c.rules||[]):'')).join('\n');
  if (field === 'allowed_effects') return value.map(e => EFFECT_NAMES[e] || e).join('、') || '无';
  if (field === 'policy') return !value ? '默认策略' : devMode ? JSON.stringify(value, null, 2) : '已按你的要求设置';
  return value.join('\n') || '无';
}
async function showTasks() {
  if (!currentConversation) return;
  const cid = currentConversation, epoch = ++taskViewEpoch, dialog = $('task-editor'), body = $('task-editor-body');
  body.replaceChildren(node('p', '', '正在读取任务…'));
  if (!dialog.open) dialog.showModal();
  try {
    const {items} = await api(`/api/conversations/${cid}/tasks`);
    if (epoch !== taskViewEpoch || cid !== currentConversation) return;
    body.replaceChildren();
    if (!items.length) body.append(node('p', '', '此对话尚未登记任务。'));
    for (const task of items) {
      const card = node('section', 'task-card');
      card.append(node('h3', '', (TASK_STATES[task.status] || task.status) + (devMode ? ` · 版本 ${task.revision} · ${task.id}` : '')));
      if (task.status==='completed') card.append(node('p','task-hint',(task.checks||[]).some(c=>c.needs_review)?'任务已复核完成，无需重复确认。':'已完成并核对无误。'));
      for (const goal of task.goals) card.append(node('p', '', goal.description));
      for (const c of task.constraints) card.append(node('p','task-hint','约束：'+c.description));
      const edit = node('button', 'text-button', '编辑目标与约束'); edit.onclick = () => editTaskForm(task, cid);
      const verify=node('button','text-button','重新核对');
      verify.onclick=async()=>{
        verify.disabled=true;
        try { await api(`/api/conversations/${cid}/tasks/${task.id}/verify`,{method:'POST',body:JSON.stringify({expected_revision:task.revision})}); await showTasks(); }
        catch(err){verify.disabled=false;card.append(node('p','task-hint',err.message));}
      };
      const resume = node('button', 'text-button', '在聊天中继续');
      // The model finds the task in its recent-task list; the user need not see the ID.
      resume.onclick = () => { dialog.close(); $('message-input').value = `继续之前的任务「${task.goals[0]?.description || ''}」，完成还没做完的部分。`; $('message-input').focus(); };
      card.append(edit, verify, resume);
      const accepted=(key,id)=>[...(task.reviews||[])].reverse().find(r=>r.revision===task.revision && r[key]?.includes(id))?.decision==='accept';
      const pendingGoals = (task.checks || []).filter(c => c.passed && c.needs_review && !accepted('goal_ids',c.goal_id)).map(c => c.goal_id);
      const pendingConstraints = task.constraints.filter(c => !c.forbidden_effects.length && !c.rules?.length && !accepted('constraint_ids',c.id)).map(c => c.id);
      if (pendingGoals.length || pendingConstraints.length) {
        const weak=(task.checks||[]).some(c=>c.conditions?.some(x=>x.kind==='tool_success'));
        const note = node('p', 'task-hint', weak ? '此任务仅记录了工具调用成功，缺少完整核验依据，保留待核实状态。认可表示你已核对结果。' : '只有仍需人工判断的结果或约束需要复核；认可表示你的判断，不会再次操作文件。');
        const feedback = taskField('复核说明（可选）');
        card.append(note, feedback.wrap);
        for (const [decision, label] of [['accept', '认可上述待复核项'], ['reject', '结果需修改']]) {
          const button = node('button', 'text-button', label);
          button.onclick = async () => {
            button.disabled = true;
            try {
              await api(`/api/conversations/${cid}/tasks/${task.id}/review`, {method:'POST', body:JSON.stringify({expected_revision:task.revision, goal_ids:pendingGoals, constraint_ids:pendingConstraints, decision, note:feedback.input.value})});
              await showTasks();
            } catch (err) { button.disabled=false; note.textContent=err.message; }
          };
          card.append(button);
        }
      }
      body.append(card);
    }
  } catch (err) { if (epoch === taskViewEpoch) body.replaceChildren(node('p', '', err.message)); }
}
function editTaskForm(task, cid) {
  const body=$('task-editor-body'); body.replaceChildren(node('h3', '', `编辑任务 · 版本 ${task.revision}`));
  body.append(node('p', 'task-hint', '先预览差异再应用。修改目标文字会保留原有完成条件，并要求复核含义是否一致；需要改变完成条件时可在聊天中重新规划。编辑不会直接改动文件。'));
  const form=node('form', 'task-form'), goalInputs=[], removedGoals=[], removedConstraints=[];
  for (const goal of task.goals) {
    const field=taskField('目标', goal.description), remove=node('input'); remove.type='checkbox';
    const label=node('label', '', '移除此目标 '); label.append(remove); field.wrap.append(label); form.append(field.wrap);
    goalInputs.push({goal, input:field.input, remove});
  }
  const extra=taskField('新增目标（可选）'); form.append(extra.wrap);
  for (const constraint of task.constraints) {
    const label=node('label', 'task-remove', `移除约束：${constraint.description} `), input=node('input'); input.type='checkbox';
    label.append(input); form.append(label); removedConstraints.push({id:constraint.id, input});
  }
  const constraint=taskField('新增约束（可选）'); form.append(constraint.wrap);
  const read=taskField('读取范围（每行一个相对目录）', task.read_roots.join('\n'));
  const write=taskField('写入范围（每行一个相对目录，留空禁止写入）', task.write_roots.join('\n'));
  const questions=taskField('待确认问题（每行一个，解决后可清空）', task.unresolved_questions.join('\n'));
  form.append(read.wrap, write.wrap, questions.wrap);
  const effects=[];
  for (const [effect, title] of Object.entries(EFFECT_NAMES)) {
    const label=node('label', 'task-effect', title+' '), input=node('input'); input.type='checkbox'; input.checked=task.allowed_effects.includes(effect);
    label.append(input); form.append(label); effects.push({effect,input});
  }
  const reason=taskField('修改原因'); reason.input.required=true; form.append(reason.wrap);
  const cancelLabel=node('label','task-effect','取消整个任务 '), cancel=node('input'); cancel.type='checkbox'; cancel.checked=!!task.cancelled; cancelLabel.append(cancel); form.append(cancelLabel);
  const preview=node('button', 'text-button', '预览修改'), apply=node('button', 'text-button', '确认应用此修改'), back=node('button', 'text-button', '返回任务列表');
  preview.type='submit'; apply.type=back.type='button'; apply.disabled=true; back.onclick=showTasks;
  const diff=node('div', 'task-diff'), message=node('p', 'task-hint'); message.setAttribute('role','status'); let editId=null;
  form.append(preview, apply, back, message, diff); body.append(form);
  form.addEventListener('input', () => { apply.disabled=true; editId=null; diff.replaceChildren(); });
  const lines=input => input.value.split('\n').map(x=>x.trim()).filter(Boolean);
  form.onsubmit=async event => {
    event.preventDefault(); preview.disabled=true; apply.disabled=true; message.textContent='正在生成修改预览…';
    try {
      const patch={expected_revision:task.revision, upsert_goals:[], remove_goal_ids:[], remove_constraint_ids:removedConstraints.filter(x=>x.input.checked).map(x=>x.id), upsert_constraints:[], read_roots:lines(read.input), write_roots:lines(write.input), allowed_effects:effects.filter(x=>x.input.checked).map(x=>x.effect), unresolved_questions:lines(questions.input), reason:reason.input.value.trim(), cancel:cancel.checked};
      for (const {goal,input,remove} of goalInputs) {
        if (remove.checked) patch.remove_goal_ids.push(goal.id);
        else if (!input.value.trim()) throw Error('目标不能为空；需要删除时请勾选“移除此目标”。');
        else if (input.value.trim() !== goal.description) patch.upsert_goals.push({...goal, description:input.value.trim(), semantic_review:true});
      }
      if (!patch.read_roots.length) throw Error('请填写至少一个读取范围。');
      if (patch.remove_goal_ids.length===task.goals.length && !extra.input.value.trim()) throw Error('请保留一个历史目标；取消整项任务可勾选“取消整个任务”。');
      if (extra.input.value.trim()) patch.upsert_goals.push({id:'user-'+crypto.randomUUID(), description:extra.input.value.trim(), conditions:[{kind:'manual_review'}], semantic_review:true});
      if (constraint.input.value.trim()) patch.upsert_constraints.push({id:'user-'+crypto.randomUUID(), description:constraint.input.value.trim(), forbidden_effects:[], rules:[]});
      const result=await api(`/api/conversations/${cid}/tasks/${task.id}/edits`,{method:'POST',body:JSON.stringify(patch)});
      diff.replaceChildren();
      for (const change of result.changes) {
        const block=node('section','task-change'); block.append(node('h4','',FIELD_NAMES[change.field]||change.field),node('pre','task-before','修改前\n'+taskValue(change.field,change.before)),node('pre','task-after','修改后\n'+taskValue(change.field,change.after))); diff.append(block);
      }
      editId=result.id; apply.disabled=false; message.textContent='请核对差异，确认后生成新版本。';
    } catch (err) { message.textContent=err.message; } finally { preview.disabled=false; }
  };
  apply.onclick=async () => {
    if (!editId) return; apply.disabled=true;
    try { await api(`/api/conversations/${cid}/tasks/${task.id}/edits/apply`,{method:'POST',body:JSON.stringify({edit_id:editId})}); await showTasks(); }
    catch (err) { message.textContent=err.message; }
  };
}
$('open-tasks').onclick=showTasks;
$('task-editor-close').onclick=()=>$('task-editor').close();
