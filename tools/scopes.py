"""Capability-local adapters. The controller has no business tool-name switch."""


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


def finding_paths(args, sb):
    return [fid for item in args['findings'] for fid in [item['file_id'], *item.get('related_file_ids', [])]]
