"""Read-only local cache/history benchmark; does not call a model or alter source files.

.venv/bin/python -m eval.latency [--conversation-id ID]
"""
import argparse
import json
import sqlite3
import time
from pathlib import Path

import yaml

from agent.history import prepare_history
from safety.sandbox import Sandbox
from tools.dedup import scan_duplicates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sandbox', type=Path)
    parser.add_argument('--conversation-id')
    parser.add_argument('--output', type=Path, default=Path('eval/results/latency_local/report.json'))
    args = parser.parse_args()
    cfg = yaml.safe_load(Path('config.yaml').read_text())
    root = args.sandbox or Path(cfg['sandbox'])
    if not root.is_dir():
        parser.error('sandbox must already exist')
    sb, scans = Sandbox(root), []
    for mode in ('text', 'text', 'near'):
        started = time.perf_counter()
        scan = scan_duplicates(sb, mode=mode)
        scans.append(dict(mode=mode, seconds=round(time.perf_counter() - started, 4),
                          stats=scan['stats'], groups=len(scan['groups'])))
    report = dict(cache=scans, api_calls=0, writes_to_sandbox=0,
                  notes='Warm process cache; local measurements only, not end-to-end model latency.')
    if args.conversation_id:
        with sqlite3.connect('file:.filebutler-ui/state.sqlite3?mode=ro', uri=True) as conn:
            row = conn.execute('SELECT history_json FROM conversations WHERE id=?', (args.conversation_id,)).fetchone()
        if not row:
            parser.error('conversation not found')
        _, report['history_projection'] = prepare_history(json.loads(row[0]), cfg)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
