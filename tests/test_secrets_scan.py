"""Secret scanning of hidden files a recycle would take along (agent/secrets_scan.py).

Fake credentials are assembled at run time, so no token-shaped literal is
committed (and push protection is not triggered).
"""
import json
import random
import string

from agent.secrets_scan import SCAN_BYTES, entropy, scan_file, scan_text, sensitive_name

import helpers as H
from test_protocol_v3 import Session, step, call, start, errors, TASK, PLAN, execute, finish

rng = random.Random(7)


def noise(n, alphabet=string.ascii_letters + string.digits):
    return ''.join(rng.choice(alphabet) for _ in range(n))


GITHUB = 'gh' + 'p_' + noise(36)
AWS_ID = 'AK' + 'IA' + noise(16, string.ascii_uppercase + string.digits)
OPENAI = 's' + 'k-' + noise(32)
PRIVATE = '-----BEGIN OPENSSH ' + 'PRIVATE KEY-----'


def test_sensitive_file_names():
    for name in ('.env', '.env.local', 'id_rsa', 'server.pem', '.netrc', '.git-credentials', 'Credentials.json'):
        assert sensitive_name(name), name
    for name in ('id_rsa.pub', '.gitignore', '.DS_Store', 'notes.txt'):
        assert sensitive_name(name) is None, name


def test_provider_formats_are_found_with_line_and_masked_prefix():
    text = '\n'.join(['# config', 'GITHUB=' + GITHUB, 'key ' + AWS_ID, 'OPENAI_KEY=' + OPENAI, PRIVATE])
    found = {f['kind']: f for f in scan_text(text)}
    assert found['GitHub 令牌']['line'] == 2 and found['GitHub 令牌']['masked'] == 'ghp_****'
    assert found['AWS 访问密钥']['line'] == 3
    assert found['OpenAI 类 API 密钥']['line'] == 4
    assert found['私钥']['line'] == 5
    assert all(GITHUB not in f['masked'] and len(f['masked']) <= 8 for f in found.values())


def test_generic_assignments_need_a_generated_looking_value():
    strong = 'api_key = ' + noise(32)
    assert scan_text(strong) and entropy(strong.split('= ')[1]) >= 3.5
    assert not scan_text('password = aaaaaaaaaaaaaaaa')  # long but not random
    assert not scan_text('password = hunter2')            # too short to judge
    assert not scan_text('DEBUG=true\nLOG_LEVEL=info')


def test_binary_files_are_not_read_as_text_and_reads_are_bounded(tmp_path):
    binary = tmp_path / '.cache'
    binary.write_bytes(b'\x00\x01' + GITHUB.encode())
    assert scan_file(binary)['binary'] and not scan_file(binary)['findings']
    late = tmp_path / '.big'
    late.write_text('x' * (SCAN_BYTES + 10) + '\nTOKEN=' + GITHUB)
    report = scan_file(late)
    assert report['truncated'] and not report['findings']  # only the first SCAN_BYTES are read


# ---------------------------------------------------------------- in the recycle flow


def leaky_fixture(root):
    (root / '项目').mkdir()
    (root / '项目/a.txt').write_text('a')
    (root / '项目/.config_backup').write_text('# backup\nGITHUB=' + GITHUB + '\n')


ASK = dict(question='项目 里还有隐藏文件，要怎么处理？', reason='scope_beyond_request', subject=['项目'],
           options=[dict(label='连同隐藏文件一起回收', consent='include_hidden'), dict(label='保留文件夹')])
PLAN_ARGS = dict(description='移出并回收', actions=[dict(effect='move', source='项目/a.txt', destination='.'),
                                                dict(effect='trash', source='项目', include_hidden=['项目/.config_backup'])])


def test_model_sees_kind_and_line_but_never_the_value(tmp_path):
    leaky_fixture(tmp_path)
    session = Session(tmp_path)
    _, client = session.run('删除项目文件夹', [step(start([dict(kind='organize', description='回收')]),
                                                 call('prepare_file_plan', PLAN_ARGS))])
    text = client.calls('prepare_file_plan')[0]['text']
    assert '第 2 行疑似GitHub 令牌' in text and '建议默认保留' in text and 'finish_turn(mode=ask)' in text
    assert GITHUB not in text and 'ghp_****' not in text
    assert (tmp_path / '项目/.config_backup').is_file()  # a finding never lets the file through


def test_question_and_card_carry_the_local_finding(tmp_path):
    leaky_fixture(tmp_path)
    session = Session(tmp_path)
    asked, _ = session.run('删除项目文件夹', [step(call('ask_user', ASK))])
    assert '本地检查：项目/.config_backup：第 2 行疑似GitHub 令牌。建议保留。' in asked.final
    assert 'ghp_****' not in asked.final  # the chat history reaches the model
    result, client = session.run('A', [step(start([dict(kind='organize', description='回收')]),
                                            call('prepare_file_plan', PLAN_ARGS, alias=PLAN)), execute(), finish()])
    assert errors(client) == []
    [request] = [r for r in session.approvals if r['tool'] == 'file_plan']
    from web_confirm import build_confirmation
    notes = build_confirmation(request, H.Sandbox(tmp_path))['notes']
    assert any(n.startswith('⚠⚠ 本地检查发现疑似密钥，建议保留') and 'ghp_****' in n for n in notes)
    assert all(GITHUB not in c['text'] for c in client.log)
    assert GITHUB not in json.dumps(result.messages, ensure_ascii=False)
