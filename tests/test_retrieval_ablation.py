"""The controlled retrieval evaluation (eval/retrieval_ablation.py and friends); synthetic data only.

Pins what makes the comparison controlled: one factor changes between neighbouring configs,
the indexed text is the same text in every config (a difference stops the run), reachability
is told apart from ranking, the gain splits into per-question parts that add up, statistics
resample whole information needs, the relabelling tool never sees the ranking code, and a
held-out set runs only frozen and only once.
"""
import itertools
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from eval import retrieval_ablation as ab
from eval import retrieval_library as library
from eval import retrieval_relabel as relabel
from eval import retrieval_stats as st
from tests.test_retrieval import pdf


# --------------------------------------------------------------------------- statistics

def test_sign_test_matches_the_binomial_by_hand():
    assert st.sign_test(9, 1) == pytest.approx(2 * (1 + 10) / 2 ** 10)
    assert st.sign_test(1, 9) == st.sign_test(9, 1)
    assert st.sign_test(5, 5) == 1.0 and st.sign_test(0, 0) == 1.0


def test_bootstrap_resamples_whole_clusters_and_is_repeatable():
    deltas, clusters = [1.0, 1.0, 0.0], ['a', 'a', 'b']
    mean, lo, hi = st.cluster_bootstrap(deltas, clusters, b=2000, seed=7)
    assert mean == pytest.approx(2 / 3)
    # whole clusters: a resample holds a twice, b twice, or one of each
    assert {round(lo, 6), round(hi, 6)} <= {0.0, round(2 / 3, 6), 1.0}
    assert st.cluster_bootstrap(deltas, clusters, b=2000, seed=7) == (mean, lo, hi)
    one = st.paired([1.0, 1.0, -0.5], ['a', 'a', 'b'], b=500)
    assert (one['clusters'], one['better'], one['worse'], one['questions']) == (2, 1, 1, 3)


def test_random_floor_matches_enumeration():
    n, g, k = 6, 2, 3
    files, gold = list(range(n)), {0, 1}
    tops = [set(p[:k]) for p in itertools.permutations(files)]
    recall = sum(len(t & gold) / min(k, g) for t in tops) / len(tops)
    success = sum(bool(t & gold) for t in tops) / len(tops)
    assert st.random_floor(g, n, k) == (pytest.approx(recall), pytest.approx(success))
    assert st.random_floor(1, 3, 5) == (1.0, 1.0)  # fewer files than places


def test_tie_aware_recall_counts_a_tie_across_the_cutoff_by_expectation():
    ranked = [('a', 3), ('b', 2), ('c', 1), ('d', 1), ('e', 1)]
    recall, success, straddle = st.tie_aware(ranked, {'d'}, 3)
    assert (recall, success, straddle) == (pytest.approx(1 / 3), pytest.approx(1 / 3), True)
    assert st.tie_aware(ranked, {'a', 'd'}, 3) == (pytest.approx((1 + 1 / 3) / 2), 1.0, True)
    assert st.tie_aware(ranked[:2], {'b'}, 3) == (1.0, 1.0, False)
    assert st.tie_aware(ranked, set(), 3) == (None, None, False)


def test_shapley_parts_add_up_and_treat_equal_factors_equally():
    v = lambda on: float({'a', 'b'} <= on) + 0.25 * ('c' in on)
    parts = st.shapley(v, ('a', 'b', 'c'))
    assert parts == {'a': pytest.approx(0.5), 'b': pytest.approx(0.5), 'c': pytest.approx(0.25)}
    assert sum(parts.values()) == pytest.approx(v({'a', 'b', 'c'}) - v(set()))


def test_factorial_effects_on_a_hand_made_table():
    v = lambda on: 2 * ('A' in on) + 3 * ('B' in on) + ('A' in on and 'B' in on)
    effects = st.factorial(v, ('A', 'B', 'C'))
    assert effects[('A',)] == pytest.approx(2.5) and effects[('B',)] == pytest.approx(3.5)
    assert effects[('A', 'B')] == pytest.approx(0.5) and effects[('C',)] == 0
    assert effects[('A', 'B', 'C')] == 0


# --------------------------------------------------------------------------- a small library

FILLER = 'This page is about something else entirely, with ordinary words that match nothing asked here.'


@pytest.fixture
def root(tmp_path):
    lib = tmp_path / 'lib'
    pdf(lib / 'papers/front.pdf', ['Graph contrastive augmentation for rumour detection.', FILLER])
    pdf(lib / 'papers/late.pdf', [FILLER, FILLER, FILLER, 'The zebra lattice threshold is reported here.'])
    pdf(lib / 'papers/other.pdf', ['Weather forecasting with recurrent networks.', FILLER])
    (lib / 'notes').mkdir()
    (lib / 'notes/memo.md').write_text('Reimbursement: hangzhou train ticket, 553 yuan.\n', encoding='utf-8')
    (lib / '.oplog').mkdir()
    (lib / '.oplog/ignored.md').write_text('hangzhou zebra graph', encoding='utf-8')
    return lib


SPEC = dict(role='dev', library=[{'name': 'x', 'source': 'x'}], queries=[
    dict(id='F', cluster='F', query='graph contrastive augmentation', relevant=[
        dict(path='papers/front.pdf', page=1, evidence='Graph contrastive augmentation')]),
    dict(id='L', cluster='L', query='zebra lattice threshold', relevant=[
        dict(path='papers/late.pdf', page=4, evidence='zebra lattice threshold')]),
    dict(id='M', cluster='M', query='hangzhou train reimbursement', relevant=[
        dict(path='notes/memo.md', page=1, evidence='hangzhou train ticket')]),
    dict(id='N', cluster='N', query='quantum chromodynamics', relevant=[]),
])


def spec():
    return json.loads(json.dumps(SPEC))


def test_library_files_leave_hidden_ones_out(root):
    assert library.files(root) == ['notes/memo.md', 'papers/front.pdf', 'papers/late.pdf', 'papers/other.pdf']
    with pytest.raises(ValueError):
        library.build(root.parent / 'x', [{'name': 'a', 'source': 'a'}, {'name': 'a', 'source': 'b'}])


def test_one_factor_at_a_time_reaches_exactly_the_expected_gold(root):
    report = ab.run(spec(), root, [40])
    assert len(report['configs']) == 8
    rows = {r['id']: r for r in report['questions']}
    assert {q: rows[q]['stratum'] for q in 'FLMN'} == {'F': 'front_pdf', 'L': 'late_pdf', 'M': 'nonpdf', 'N': 'no_relevant'}
    outcome = lambda q, cfg: next(iter(rows[q][ab.name(cfg)]['outcome'].values()))
    base, pages, formats = ab.config('pdf', 2, None), ab.config('pdf', None, None), ab.config('all', 2, None)
    assert [outcome(q, base) for q in 'FLM'] == ['hit', 'unreachable', 'unreachable']
    assert [outcome(q, pages) for q in 'FLM'] == ['hit', 'hit', 'unreachable']      # whole PDFs: the late page
    assert [outcome(q, formats) for q in 'FLM'] == ['hit', 'unreachable', 'hit']    # all formats: the note
    assert report['configs'][ab.name(base)]['files'] == 3 and report['configs'][ab.name(formats)]['files'] == 4
    assert all('.oplog' not in p for r in rows.values() for p in r[ab.name(formats)]['top'])
    # each question's gain goes to the factor that made its gold reachable, and the parts add up
    a = report['analysis']['40']
    assert a['late_pdf']['shapley']['pages']['mean'] == pytest.approx(1.0)
    assert a['nonpdf']['shapley']['formats']['mean'] == pytest.approx(1.0)
    whole = a['all_with_gold']
    assert sum(whole['shapley'][p]['mean'] for p in ab.PLAYERS) == pytest.approx(whole['total']['mean'])
    assert whole['ranking_on_reachable']['gold'] == 1  # only F's gold is reachable in both baseline and target
    assert 'Shapley' in ab.summary({**report, 'environment': dict(set='s', set_sha256='0' * 64, git_head='abcdef12', git_dirty=False,
                                                                     macos='15', ocr='o', extract='e', k1=1.2, b=0.75, seed=1)})


def test_different_text_for_the_first_two_pages_stops_the_run(root, monkeypatch):
    import agent.extract as extract
    real = extract.units

    def units(sb, path, *, start=1, pages=2):
        out = real(sb, path, start=start, pages=pages)
        return out[:1] + [out[1] + ' changed'] if pages == 2 and path.suffix == '.pdf' else out
    monkeypatch.setattr(extract, 'units', units)
    with pytest.raises(ab.EvalError, match='前两页'):
        ab.run(spec(), root, [40])


def test_pending_ocr_or_other_text_in_a_config_stops_the_run():
    cfg = ab.config('all', None, None)
    with pytest.raises(ab.EvalError, match='OCR'):
        ab.check_config_text(cfg, {}, {'a.jpg': ((1, 1), 'pending_ocr', 1, 0)}, {})
    paper = type('P', (), {'pages': ['other']})()
    with pytest.raises(ab.EvalError, match='不一致'):
        ab.check_config_text(cfg, {'a.md': paper}, {}, {'a.md': ['text']})


def test_the_text_manifest_catches_a_changed_corpus(tmp_path):
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(ab.manifest({'a.md': ['one'], 'b.pdf': ['p1', 'p2']})))
    ab.check_manifest({'a.md': ['one'], 'b.pdf': ['p1', 'p2']}, path)
    with pytest.raises(ab.EvalError, match='b.pdf'):
        ab.check_manifest({'a.md': ['one'], 'b.pdf': ['p1', 'p2 edited']}, path)


def test_validation_flags_a_late_label_on_front_text_and_unreviewed_empty_questions(root):
    s = spec()
    s['queries'][0]['relevant'][0].update(page=2, evidence='This page is about something else')  # on page 1 and 2 of front.pdf
    s['queries'][1]['relevant'][0].update(path='papers/front.pdf', page=2, evidence='with ordinary words')
    errors, notes = ab.validate(s, root)
    assert any('N: 无相关题' in e for e in errors)
    assert not any('front_pdf: ' in e for e in errors) and any('front_pdf: ' in n for n in notes)  # dev: counts only
    errors, _ = ab.validate(dict(s, role='heldout'), root)
    assert any('front_pdf: ' in e for e in errors)  # held-out: far below the minimum of 15
    s['queries'][1]['relevant'][0].update(page=3)
    errors, _ = ab.validate(dict(s, minimums={k: 0 for k in ab.DEFAULT_MINIMUMS}), root)
    assert any('不存在' in e or '证据不匹配' in e for e in errors)  # front.pdf has no page 3


def test_the_dev_rule_picks_recall_then_mrr_then_speed():
    def cfg(c, r, rr, ms):
        return {f'all_pall_c{c}': dict(recall=r, rr=rr)}, {f'all_pall_c{c}': dict(query_ms_mean=ms)}
    rows, configs = {}, {}
    for c, r, rr, ms in [(250, 0.7, 0.5, 1), (500, 0.8, 0.6, 3), (1000, 0.8, 0.6, 2)]:
        a, b = cfg(c, r, rr, ms)
        rows.update(a), configs.update(b)
    report = dict(questions=[dict(relevant=['x'], **rows)], configs=configs)
    assert ab.choose_chunk(report, [250, 500, 1000]) == 1000  # same recall and MRR, faster


# --------------------------------------------------------------------------- freezing

def test_a_held_out_set_runs_only_frozen_and_once(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    (repo / 'eval').mkdir(parents=True)
    run = lambda *a: subprocess.run(['git', *a], cwd=repo, check=True, capture_output=True)
    run('init', '-q')
    run('config', 'user.email', 't@example.invalid')
    run('config', 'user.name', 't')
    heldout, choice = repo / 'eval/set.yaml', repo / 'eval/choice.yaml'
    heldout.write_text('role: heldout\n')
    choice.write_text('rule: x\nchosen_chunk: 500\n')
    monkeypatch.setattr(ab, 'ROOT', repo)
    results = repo / 'results'
    results.mkdir()
    with pytest.raises(ab.EvalError, match='没有提交'):
        ab.guard_heldout(heldout, choice, 500, results, 'sha', '')
    run('add', '.')
    run('commit', '-qm', 'freeze')
    ab.guard_heldout(heldout, choice, 500, results, 'sha', '')
    with pytest.raises(ab.EvalError, match='不一致'):
        ab.guard_heldout(heldout, choice, 250, results, 'sha', '')
    (results / 'first').mkdir()
    (results / 'first/report.json').write_text(json.dumps({'environment': {'set_sha256': 'sha'}}))
    with pytest.raises(ab.EvalError, match='已经跑过'):
        ab.guard_heldout(heldout, choice, 500, results, 'sha', '')
    ab.guard_heldout(heldout, choice, 500, results, 'sha', 'the first run used a broken library')
    heldout.write_text('role: heldout\nqueries: []\n')
    with pytest.raises(ab.EvalError, match='没有提交'):
        ab.guard_heldout(heldout, choice, 500, results, 'sha', 'x')


def test_relabelling_never_imports_the_ranking_code():
    out = subprocess.run([sys.executable, '-c', 'import sys, eval.retrieval_relabel; print("tools.retrieval" in sys.modules)'],
                         cwd=library.ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == 'False'


def test_pattern_scan_lists_every_occurrence_with_context(root):
    found = relabel.scan(root, [dict(id='M', patterns=['Hangzhou', '553 yuan']), dict(id='Z', patterns=['nowhere'])])
    assert [(rel, page, pattern) for rel, page, pattern, _ in found['M']] == [('notes/memo.md', 1, 'Hangzhou'), ('notes/memo.md', 1, '553 yuan')]
    assert 'train ticket' in found['M'][0][3] and found['Z'] == []
    assert '## notes/memo.md' in relabel.dump(root, 'notes') and 'papers/' not in relabel.dump(root, 'notes')


def test_the_dev_draft_moves_the_old_set_onto_one_library(tmp_path):
    draft = relabel.draft_dev()
    queries = {q['id']: q for q in draft['queries']}
    assert len(queries) == 61 and draft['role'] == 'dev'
    assert all(g['path'].split('/', 1)[0] in ('papers', 'mixed') for q in queries.values() for g in q['relevant'])
    assert queries['Q14']['cluster'] == queries['Q13']['cluster'] == 'Q13'  # one need asked in two languages
    assert queries['Q10']['cluster'] == 'Q10' and not queries['Q10']['relevant']
    path = tmp_path / 'dev.yaml'
    path.write_text(yaml.safe_dump(draft, allow_unicode=True, sort_keys=False), encoding='utf-8')
    loaded = ab.load_set(path)  # the draft is a loadable set
    assert loaded['library'][1]['builder'] == 'eval.retrieval_mixed' and len(loaded['queries']) == 61
