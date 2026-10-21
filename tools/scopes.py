"""Capability-local adapters. The controller has no business tool-name switch."""
from pathlib import Path


def paths(*fields):
    def resolve(args, sb):
        return [args[key] for key in fields if isinstance(args.get(key), str)]
    return resolve


def no_paths(args, sb):
    return []


def catalog_scope(args, sb):
    if args.get('catalog_id'):
        return [sb.artifacts.data['catalogs'][args['catalog_id']]['path']]
    return [args.get('path', '.')]


def scan_scope(args, sb):
    if args.get('scan_id'):
        from tools.dedup import get_scan
        return [get_scan(sb, args['scan_id'])['path']]
    return [args.get('path', '.')]


def scan_files(args, sb):
    from tools.dedup import get_scan
    return list(get_scan(sb, args['scan_id'])['fingerprints'])


def classification_paths(args, sb):
    from tools.classification import _plan, _move_plan
    if args.get('plan_id', 'latest') == 'latest':
        raise ValueError('执行必须提供精确 plan_id，不能隐式使用 latest')
    pid, plan = _plan(sb.artifacts, args['plan_id'])
    return [path for move in _move_plan(sb, pid, plan) for path in (move['src'], move['dst'])]


def finding_paths(args, sb):
    return [fid for item in args['findings'] for fid in [item['file_id'], *item.get('related_file_ids', [])]]


def mkdir_operations(args, sb):
    from agent.constraints import creations
    return creations(sb, args['path'])


def move_operations(args, sb):
    from agent.constraints import operation
    return [operation('move', args['src'], (Path(args['dst_dir']) / Path(args['src']).name).as_posix())]


def rename_operations(args, sb):
    from agent.constraints import operation
    return [operation('rename', args['path'], (Path(args['path']).parent / args['new_name']).as_posix())]


def trash_operations(args, sb):
    from agent.constraints import operation
    return [operation('trash', args['path'])]
