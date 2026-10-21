"""Attribute every rejected tool call in live traces to a cause, and count protocol waste.

Read-only over ``eval/results/<run>/<case>/turn_N.trace.jsonl``; nothing is
written next to frozen results. Used to compare protocols (v2 / v3) on the same
terms: which rejections each one produces, and how many model requests did
nothing but repair the protocol.

    .venv/bin/python -m eval.protocol_errors eval/results/policy_live_20261021_05_regression [...] [--json out.json]

Categories are matched on the local rejection text, in order; the first match
wins. They describe where a rejection comes from, not whether it was justified:
a user limit that correctly stops a plan is still a ``rule`` rejection.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

CATEGORIES = [
    ('format', '参数 JSON 写坏', r'Extra data|参数不是合法 JSON|Expecting .* delimiter|Invalid control character'),
    ('rule', '约束规则不成立或写法错误', r'约束 \S+ 未满足|操作数量上限|数量约束需要 limit|selector=|same_extension|create 没有|trash 没有'),
    ('scope_roots', '写入范围未覆盖对象', r'写入范围外的对象|写入范围不包含'),
    ('completion_contract', '完成条件与清单或能力不符', r'路径映射|执行前验收清单|tool_success 作为完成条件|只能由.*回执满足|完成条件路径'),
    ('destination_shape', '目标路径写法或目标冲突', r'目标重复或名称折叠|目标已存在，不会覆盖'),
    ('stale_revision', '任务修订后计划过期', r'任务版本已'),
    ('missing_step', '缺少步骤归属', r'begin_step'),
    ('text_constraint', '文本约束拦截', r'文本约束'),
    ('source_gone', '来源已不存在', r'来源不存在|源文件版本变化或不存在|不存在。可以先用'),
    ('refused', '用户拒绝', r'用户拒绝'),
    ('needs_edit', '应走任务编辑确认流程', r'必须通过任务编辑确认|必须先预览任务编辑'),
    ('schema_shape', '参数结构不符 schema', r'Extra inputs are not permitted|extra_forbidden|Field required|validation error'),
]
WRITE_TOOLS = {'execute_file_plan', 'deduplicate', 'apply_dedup', 'apply_classification', 'move', 'rename', 'trash', 'make_dir'}
# Calls that only maintain the protocol: in the first turn of a case an edit is
# the model repairing its own contract (later turns carry user corrections).
SELF_EDIT = {'edit_task', 'apply_task_edit', 'revise_task'}
BOOKKEEPING = {'begin_step', 'inspect_file_plan'}
# A count limit the user stated; "保留一份" names what to keep, not a limit.
STATED_NUMBER = re.compile(r'最多|至多|不超过|不多于|上限|\d+ ?[个份篇项]')


def classify(text):
    for label, _, pattern in CATEGORIES:
        if re.search(pattern, text):
            return label
    return 'other'


def load(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]


def analyse_trace(events, first_turn=True):
    instruction = next((e.get('instruction', '') for e in events if e.get('event') == 'start'), '')
    requests = sum(e.get('event') == 'llm_request' and e.get('phase') == 'end' for e in events)
    calls = [e for e in events if 'tool' in e and 'event' not in e]
    errors, steps, seen = [], {}, {}
    duplicates = dict(total=0, after_state_change=0)
    wrote, post_write_checks, self_limits = False, 0, 0
    for index, call in enumerate(calls):
        result = str(call.get('result', ''))
        failed = result.startswith('ERROR')
        if failed:
            errors.append(dict(tool=call['tool'], category=classify(result), text=result[:300]))
        waste = failed or call['tool'] in BOOKKEEPING or (first_turn and call['tool'] in SELF_EDIT)
        steps.setdefault(call.get('step'), []).append(waste)
        key = (call['tool'], call.get('args'))
        if key in seen:
            duplicates['total'] += 1
            # A retry after a successful write or contract change is not a blind loop.
            changed = any(not str(c.get('result', '')).startswith('ERROR') and
                          (c['tool'] in WRITE_TOOLS or c['tool'] in SELF_EDIT | BOOKKEEPING | {'set_task', 'start_task'})
                          for c in calls[seen[key] + 1:index])
            duplicates['after_state_change'] += changed
        seen[key] = index
        if call['tool'] in WRITE_TOOLS and not failed:
            wrote = True
        elif wrote and call['tool'] in {'list_dir', 'file_info'}:
            post_write_checks += 1
        if call['tool'] in {'set_task', 'start_task'} and not STATED_NUMBER.search(instruction):
            args = call.get('args') or ''
            self_limits += '"max_count"' in args or re.search(r'"max_files":\s*\d', args) is not None
    return dict(requests=requests, calls=len(calls), errors=errors,
                protocol_only_requests=sum(all(v) for v in steps.values()),
                duplicates=duplicates, post_write_checks=post_write_checks,
                unrequested_count_limits=self_limits)


def analyse(dirs):
    total = Counter()
    categories, by_tool = Counter(), Counter()
    duplicates = Counter()
    cases = []
    for folder in dirs:
        for trace in sorted(Path(folder).glob('*/turn_*.trace.jsonl')):
            turn = int(trace.name.split('_')[1].split('.')[0])
            row = analyse_trace(load(trace), first_turn=turn == 1)
            for key in ('requests', 'calls', 'protocol_only_requests', 'post_write_checks', 'unrequested_count_limits'):
                total[key] += row[key]
            duplicates.update(row['duplicates'])
            categories.update(e['category'] for e in row['errors'])
            by_tool.update(e['tool'] for e in row['errors'])
            cases.append(dict(trace=str(trace), **{k: row[k] for k in ('requests', 'protocol_only_requests')},
                              errors=[e['category'] for e in row['errors']]))
    total['errors'] = sum(categories.values())
    return dict(totals=dict(total), protocol_only_share=round(total['protocol_only_requests'] / total['requests'], 3)
                if total['requests'] else None,
                duplicates=dict(duplicates), categories=dict(categories.most_common()),
                errors_by_tool=dict(by_tool.most_common()), cases=cases)


def markdown(summary):
    names = {label: desc for label, desc, _ in CATEGORIES} | {'other': '其他'}
    t = summary['totals']
    lines = [f"请求 {t['requests']}，工具调用 {t['calls']}，被拒绝 {t.get('errors', 0)}；"
             f"纯协议纠错请求 {t['protocol_only_requests']}（{t['protocol_only_requests'] / max(t['requests'], 1):.1%}）。",
             f"完全相同的重复调用 {summary['duplicates'].get('total', 0)} 次，其中 "
             f"{summary['duplicates'].get('after_state_change', 0)} 次发生在状态变化之后；"
             f"写入后复查 {t['post_write_checks']} 次；用户未提数量时自行设置数量上限 {t['unrequested_count_limits']} 次。",
             '', '| 类别 | 说明 | 次数 |', '| --- | --- | ---: |']
    lines += [f"| {k} | {names[k]} | {v} |" for k, v in summary['categories'].items()]
    return '\n'.join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('dirs', nargs='+', type=Path)
    ap.add_argument('--json', type=Path, help='also write the full summary here (outside frozen result folders)')
    args = ap.parse_args(argv)
    summary = analyse(args.dirs)
    if args.json:
        if any(args.json.resolve().is_relative_to(d.resolve()) for d in args.dirs):
            raise SystemExit('不写入被分析的结果目录')
        args.json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(markdown(summary))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
