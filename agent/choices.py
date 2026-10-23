"""The user's decisions, as one table.

Each decision is a row: ``kind`` what was decided, ``target`` the path or part it
is about, ``source`` how it was given and ``revision`` the task version it binds.

kind      target           granted by
hidden    folder           recycling the hidden files in that folder (2026-10-22)
overwrite file             replacing a same-name file (2026-10-22)
read      file or folder   reading a sensitive file, for this task only (2026-10-22)
choice    option key       the chosen option was carried out; completion follows its scope (2026-10-22)
skip      part id          the chosen option leaves this part out

source is 'option' (a chosen letter) or 'text' (a free-text reply, which the
confirmation card must then confirm). A read grant needs an option: a read has no
card to catch a misread reply. ``revision`` is set for 'choice' and 'skip':
completion is judged by the chosen scope only in the task version it was chosen in.
``note`` is the option as the user saw it, "A（label）".
"""
KINDS = ('hidden', 'overwrite', 'read', 'choice', 'skip')


def row(kind, target, source, revision=None, note=''):
    assert kind in KINDS and source in ('option', 'text')
    return dict(kind=kind, target=target, source=source, revision=revision, note=note)


def from_answer(answered):
    """Decisions in the user's reply to the last ask_user (None: no question was answered)."""
    answered = answered or {}
    chosen = answered.get('chosen') or {}
    subject = answered.get('subject', [])
    if chosen:
        rows = [row('hidden', f, 'option') for f in subject if chosen.get('consent') == 'include_hidden']
        rows += [row('overwrite', t, 'option') for t in chosen.get('overwrite') or []]
        rows += [row('read', p, 'option') for p in chosen.get('read_grant') or []]
        return rows
    if answered:
        # Free text: may be consent to recycle hidden files or to overwrite; the card confirms.
        return [row(kind, f, 'text') for kind in ('hidden', 'overwrite') for f in subject]
    return []


def of(rows, kind):
    """{target: source} of one kind (grants, which hold for the whole task)."""
    return {r['target']: r['source'] for r in rows if r['kind'] == kind}


def chosen_scope(task):
    """(option note, part ids it left out) chosen in the task's current revision, else (None, set())."""
    rows = [r for r in task.get('decisions', []) if r['revision'] == task['revision']]
    note = next((r['note'] for r in rows if r['kind'] == 'choice'), None)
    return note, {r['target'] for r in rows if r['kind'] == 'skip'} if note else set()


def record_choice(task, key, label, excluded):
    """Replace any earlier choice of this task with the one just carried out."""
    note = f'{key}（{label}）'
    rows = [r for r in task.get('decisions', []) if r['kind'] not in ('choice', 'skip')]
    task['decisions'] = rows + [row('choice', key, 'option', task['revision'], note)] + \
        [row('skip', g, 'option', task['revision'], note) for g in excluded]


def migrate(task):
    """Tasks saved before 2026-10-23 kept these decisions as chosen_scope / sensitive_grants."""
    scope = task.pop('chosen_scope', None)
    grants = task.pop('sensitive_grants', None)
    if scope is None and grants is None: return task
    rows = task.setdefault('decisions', [])
    rows += [row('read', g, 'option') for g in grants or []]
    if scope:
        note = f"{scope['key']}（{scope['label']}）"
        rows += [row('choice', scope['key'], 'option', scope['revision'], note)]
        rows += [row('skip', g, 'option', scope['revision'], note) for g in scope['excluded']]
    return task
