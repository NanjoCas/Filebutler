"""Sensitive files (user decision 2026-10-22): contents are withheld from the model
unless the user granted reading that file, by choosing an option, in this task."""
import json

import pytest

from agent.artifacts import ArtifactStore
from agent.sensitive import content_kinds, name_kind, reasons, ID_WEIGHTS
from agent.task_runtime import TaskRuntime
from safety.sandbox import Sandbox
from tools import registry
from tools.registry import ToolError

import helpers as H
from test_protocol_v3 import Session, step, call, start, errors


def id_number(body='11010519491231002'):
    return body + '10X98765432'[sum(int(d) * w for d, w in zip(body, ID_WEIGHTS)) % 11]


def card_number(body='622202123456789'):
    for check in '0123456789':
        digits = body + check
        total = sum((n - 9 if n > 9 else n) for n in
                    (int(d) * (2 if i % 2 else 1) for i, d in enumerate(reversed(digits))))
        if total % 10 == 0:
            return digits


def test_rules_need_valid_numbers_and_skip_common_title_words():
    assert '身份证号' in content_kinds(f'证件号码：{id_number()}')
    assert content_kinds(f'编号 {id_number()[:-1]}0' if id_number()[-1] != '0' else '编号 110105194912310021') == []
    assert '银行卡号' in content_kinds(f'卡号 {card_number()}')
    assert content_kinds('DOI 10.1234/5678 and 1234567890123456') == []  # fails Luhn / prefix
    assert content_kinds('token = ghp_' + 'a' * 36) == ['GitHub 令牌']
    assert name_kind('张三身份证.jpg') == '证件' and name_kind('2025 工资条.xlsx') == '财务记录'
    assert name_kind('基于深度学习的故障诊断.pdf') is None and name_kind('Smart Contract Audit.pdf') is None
    assert name_kind('.env') == '环境变量文件'


def runtime(tmp_path):
    sb = Sandbox(tmp_path); sb.artifacts = ArtifactStore(sb)
    r = TaskRuntime('读取', dict(protocol='v3'), sb, 'one'); sb.runtime = r
    return r, sb


def read(sb, path, **kw):
    return registry.dispatch('read_file', json.dumps(dict(path=path, **kw)), sb)


@pytest.mark.parametrize('name,text', [('合同.txt', '甲方乙方'), ('notes.txt', f'客户身份证 {id_number()}')])
def test_read_file_withholds_sensitive_contents_until_granted(tmp_path, name, text):
    (tmp_path / name).write_text(text)
    r, sb = runtime(tmp_path)
    refused = read(sb, name)
    assert '敏感文件' in refused and 'ask_user' in refused and text not in refused
    r.read_grants = [name]
    assert text in read(sb, name)


def test_grant_covers_only_the_named_file(tmp_path):
    (tmp_path / '合同A.txt').write_text('A'); (tmp_path / '合同B.txt').write_text('B')
    r, sb = runtime(tmp_path)
    r.read_grants = ['合同A.txt']
    assert '<file_content>' in read(sb, '合同A.txt') and '敏感文件' in read(sb, '合同B.txt')


def test_search_and_classification_cards_hide_sensitive_text(tmp_path):
    H.pdf(tmp_path / '文献/paper.pdf', 'Abstract: graph neural networks for traffic forecasting.')
    H.pdf(tmp_path / '文献/客户合同.pdf', 'Abstract: graph neural networks contract between parties, secret terms.')
    r, sb = runtime(tmp_path)
    for view in ('evidence', 'detail'):
        text = registry.dispatch('search_content', json.dumps(dict(query='graph neural networks', path='文献', view=view)), sb)
        assert 'secret terms' not in text and '敏感文件：文件名像合同' in text and 'traffic' in text
    cards = registry.dispatch('paper_profiles', json.dumps(dict(path='文献')), sb)
    assert 'secret terms' not in cards and '敏感文件：文件名像合同' in cards and 'traffic' in cards


SENSITIVE_ASK = dict(question='要读取 合同.txt 的内容吗？', reason='scope_beyond_request', subject=['合同.txt'], options=[
    dict(label='读取合同.txt', actions=[dict(effect='read', path='合同.txt')]),
    dict(label='不读取，只按文件名整理', actions=[])])


def test_choosing_the_read_option_grants_it_for_the_task(tmp_path):
    (tmp_path / '合同.txt').write_text('付款期限三十天')
    session = Session(tmp_path)
    asked, _ = session.run('合同里的付款期限是多久？', [step(call('ask_user', SENSITIVE_ASK))])
    assert asked.report['choices'][0]['read_grant'] == ['合同.txt'] and '⚠ 授权读取敏感文件 合同.txt' in asked.final
    assert not asked.report['choices'][1].get('read_grant')
    result, client = session.run('A', [step(start([dict(kind='answer', description='付款期限')]),
                                            call('read_file', dict(path='合同.txt')))], final='三十天')
    assert errors(client) == [] and '付款期限三十天' in client.calls('read_file')[0]['text']
    task = session.sb.runtime.store.get(session.sb.runtime.current['id'])
    assert task['sensitive_grants'] == ['合同.txt']


@pytest.mark.parametrize('reply', ['B', '可以读'])
def test_declining_or_a_free_text_reply_grants_nothing(tmp_path, reply):
    (tmp_path / '合同.txt').write_text('付款期限三十天')
    session = Session(tmp_path)
    session.run('合同里的付款期限是多久？', [step(call('ask_user', SENSITIVE_ASK))])
    _, client = session.run(reply, [step(call('read_file', dict(path='合同.txt')))], final='')
    assert '敏感文件' in client.calls('read_file')[0]['text'] and '付款期限三十天' not in client.calls('read_file')[0]['text']
