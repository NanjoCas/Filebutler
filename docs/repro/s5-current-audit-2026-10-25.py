"""S5 audit replays: no API calls, temporary synthetic library, no production edits.

Written against 035567c to show four gaps. After the fixes of 2026-10-25 the assertions
state the fixed behaviour (P2-A, P1-A, P1-B); reply_contains_choices stays a known limit."""
import sys
import tempfile
import json
from pathlib import Path

REPO=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(REPO/'tests'),str(REPO)]
from helpers import Session,files_fixture,step,call
from test_protocol_v3 import start,execute,PLAN,ARCHIVE_A
from test_answer_replays import TEXT_OPTIONS

with tempfile.TemporaryDirectory(prefix='filebutler-s5-audit-') as directory:
    base=Path(directory)
    def session(name):
        root=base/name
        root.mkdir()
        files_fixture(root)
        return Session(root)

    s=session('reply_question')
    result,client=s.run('删掉所有测试文件',[
        step(call('finish_turn',dict(mode='reply',reply=TEXT_OPTIONS)))])
    assert result.stop_reason=='answered'
    assert not result.report.get('question_id') and not result.report.get('choices')
    print(json.dumps(dict(case='reply_contains_choices',stop=result.stop_reason,
        question_id=result.report.get('question_id'),choices=result.report.get('choices'),
        final_contains_choices='A. ' in result.final),ensure_ascii=False))

    s=session('ask_check')
    result,client=s.run('归档a',[
        step(start([dict(id='g',kind='organize',description='归档a')]),
             call('finish_turn',dict(mode='ask',question='去哪？',reason='missing_criterion',
                  options=[dict(label='Archive',actions=[]),dict(label='不动',actions=[])],
                  unfinished=[dict(part_id='DOES_NOT_EXIST',reason='x')],
                  evidence_bindings=[dict(part='DOES_NOT_EXIST',refs=['Enotreal'])])))])
    refused=client.calls('finish_turn')[0]
    assert refused['status']=='error' and 'mode=ask 只提问并等待用户' in refused['text']
    print(json.dumps(dict(case='ask_refuses_nonask_fields',first_call=refused['status'],
        stop=result.stop_reason),ensure_ascii=False))

    s=session('registered_reply')
    requested='额外回答：a属于项目红色，这是用户要求的说明。'
    result,client=s.run('归档a并解释所属项目',[
        step(start([dict(kind='organize',description='归档a')]),
             call('prepare_file_plan',dict(description='归档',actions=ARCHIVE_A),alias=PLAN)),
        execute(),step(call('finish_turn',dict(mode='reply',reply=requested)))])
    assert result.stop_reason=='done'
    assert result.final.count(requested)==1
    assert (s.sb.root/'Archive/a.txt').exists()
    print(json.dumps(dict(case='registered_reply_shown',stop=result.stop_reason,
        requested_in_final=True,final=result.final),ensure_ascii=False))

    s=session('invalid_terminal_loop')
    invalid=[step(call('finish_turn',dict(mode='ask',reply='draft'+str(i)))) for i in range(8)]
    result,client=s.run('问一下',invalid,final='ok')
    errors=sum(row['status']=='error' for row in client.log)
    assert result.stop_reason=='repair_limit' and client.requests==s.cfg.get('task_repair_limit',6)
    print(json.dumps(dict(case='invalid_terminal_bounded_by_no_progress',
        requests=client.requests,errors=errors,stop=result.stop_reason,
        configured_repair_limit=s.cfg.get('task_repair_limit')),ensure_ascii=False))
