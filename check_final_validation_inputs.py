#!/usr/bin/env python3
"""Offline check of two unresolved task IDs and the current extraction source.

No API calls, token prompts, source imports or database operations. Reads the
fixed saved comparison; writes only a new small audit folder. This is a
diagnostic, not a completeness certificate or a fresh extraction.
"""
import argparse
import ast
import csv
import difflib
import hashlib
import io
import json
import tokenize
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

TARGETS = ('MAAAAAEPJzKc', 'MAAAAAEQ0PI6')
TASKS = ('parent_task_id', 'child_task_id', 'grandchild_task_id',
         'baby_task_id', 'grandbaby_task_id', 'great_grandbaby_task_id')
NULLS = {'', 'none', 'nan', 'null', 'nat', '<na>', 'placeholder'}
REPORT = 'output/effort_reconciliation/20260929T172437_318678Z'
csv.field_size_limit(16 * 1024 * 1024)


def ident(value):
    value = (value or '').strip()
    return '' if value.lower() in NULLS else value


def fingerprint(path):
    before = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('File changed while reading: ' + path.name)
    return digest.hexdigest()


def rows(path, required):
    with path.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f, strict=True)
        names = [n.strip() for n in (reader.fieldnames or [])]
        if not names or len(names) != len(set(names)) or '' in names:
            raise ValueError('Invalid CSV headers: ' + path.name)
        if not set(required).issubset(names):
            raise ValueError('Missing required CSV headers: ' + path.name)
        reader.fieldnames = names
        for row in reader:
            if None in row or any(v is None for v in row.values()):
                raise ValueError('Malformed CSV row: ' + path.name)
            yield row


def resolve_input(root, audit):
    original = Path(audit['path']).expanduser()
    candidates = [original, root / original.name]
    if 'output' in original.parts:
        candidates.insert(1, root.joinpath(*original.parts[original.parts.index('output'):]))
    for path in dict.fromkeys(candidates):
        if path.is_file() and fingerprint(path) == audit['sha256']:
            return path
    raise ValueError('No unchanged saved input found for ' + original.name)


def scan(path, targets):
    before = fingerprint(path)
    counts = Counter()
    found = {t: {'metadata_rows': 0, 'hierarchy_rows': 0,
                 'eligible_metadata_rows': 0, 'direct_container_ids': set(),
                 'effort_values': set(), 'status_values': set(),
                 'scope_values': set()} for t in targets}
    total = 0
    for row in rows(path, ['key', 'id', 'effortAllocation_totalEffort', *TASKS]):
        total += 1
        key, container = ident(row['key']), ident(row['id'])
        counts[container] += 1
        hierarchy = [ident(row[k]) for k in TASKS]
        deepest = next((v for v in reversed(hierarchy) if v), '')
        for task in targets:
            record = found[task]
            if task in hierarchy:
                record['hierarchy_rows'] += 1
            if key != task:
                continue
            record['metadata_rows'] += 1
            record['eligible_metadata_rows'] += int(key == deepest and key != container)
            if container:
                record['direct_container_ids'].add(container)
            record['effort_values'].add(row['effortAllocation_totalEffort'].strip())
            record['status_values'].add(row.get('status', '(column absent)').strip())
            record['scope_values'].add(row.get('scope', '(column absent)').strip())
    if fingerprint(path) != before:
        raise ValueError('Saved input changed during scan: ' + path.name)
    for record in found.values():
        for key, value in list(record.items()):
            if isinstance(value, set):
                record[key] = sorted(value)
    return {'path': str(path), 'sha256': before, 'data_rows': total,
            'tasks': found}, counts


def source_index(path):
    if not path.is_file():
        return {'status': 'SOURCE_NOT_FOUND', 'path': str(path)}
    before = fingerprint(path)
    raw = path.read_bytes()
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    try:
        tree = ast.parse(raw.decode(encoding))
    except SyntaxError as e:
        raise ValueError('Source syntax error at line ' + str(e.lineno)) from None
    selected = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(word in node.name.lower() for word in ('task', 'wrike', 'fetch')):
                selected.append({'function': node.name, 'start': node.lineno,
                                 'end': node.end_lineno})
    writes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, 'id', getattr(node.func, 'attr', ''))
            if name in ('get_snowflake_connection', 'write_pandas'):
                writes.append({'function': name, 'line': node.lineno})
    if fingerprint(path) != before:
        raise ValueError('Source changed during inspection.')
    return {'status': 'PARSED_NOT_EXECUTED', 'path': str(path),
            'sha256': before, 'function_locations': sorted(selected, key=lambda x:x['start']),
            'snowflake_calls': writes,
            'limitation': 'Names and line locations only; no source values printed. Not proof of production version or request completeness.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--source', type=Path)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    report = args.report.expanduser().resolve() if args.report else root / REPORT
    detail_path = report / 'effort_reconciliation_details.json'
    detail_hash = fingerprint(detail_path)
    details = json.loads(detail_path.read_text(encoding='utf-8-sig'))
    name = details['comparisons']['current_snowflake_vs_saved_akash']['task_difference_file']
    if Path(name).name != name:
        raise ValueError('Unexpected difference filename.')
    difference = report / name
    difference_hash = fingerprint(difference)
    exceptions = {}
    for row in rows(difference, ['task_id', 'difference_type', 'baseline_effort_hours']):
        task = ident(row['task_id'])
        if not task or task in exceptions:
            raise ValueError('Invalid or duplicate difference task ID.')
        exceptions[task] = row
    for task in TARGETS:
        if task not in exceptions:
            near = difflib.get_close_matches(task, exceptions, n=3, cutoff=0.7)
            raise ValueError('Exact task ID absent: ' + task + '. Possible spelling matches (not selected): ' + ', '.join(near))
        row = exceptions[task]
        if row['difference_type'] != 'BASELINE_ONLY_OUTSIDE_56' or Decimal(row['baseline_effort_hours']) != 0:
            raise ValueError('Expected saved zero-effort outside-56 exception: ' + task)
    datasets, container_counts = {}, {}
    for label in ('surya', 'akash', 'snowflake'):
        path = resolve_input(root, details['datasets'][label]['input'])
        datasets[label], container_counts[label] = scan(path, TARGETS)
    relationships = {}
    for task in TARGETS:
        containers = sorted({c for d in datasets.values() for c in d['tasks'][task]['direct_container_ids']})
        relationships[task] = {c: {label: container_counts[label].get(c, 0)
                                    for label in datasets} for c in containers}
    source = source_index(args.source.expanduser().resolve() if args.source else root/'Wrike_Data_local_validation.py')
    if fingerprint(detail_path) != detail_hash or fingerprint(difference) != difference_hash:
        raise ValueError('Saved report changed during inspection.')
    result = {'checked_at_utc': datetime.now(timezone.utc).isoformat(),
              'details_sha256': detail_hash, 'difference_sha256': difference_hash,
              'targets': list(TARGETS), 'datasets': datasets,
              'container_row_counts': relationships, 'source': source,
              'limitation': 'Saved-input and source-location diagnostic only. No API or fresh completeness validation.'}
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    output = root/'output/final_validation_inputs'/stamp
    output.mkdir(parents=True, exist_ok=False)
    (output/'input_check.json').write_text(json.dumps(result, indent=2)+'\n')
    lines = ['OFFLINE CHECK: no API calls or source execution.']
    for task in TARGETS:
        lines.append('\nTASK '+task)
        for label, d in datasets.items():
            r = d['tasks'][task]
            lines.append('{}: metadata rows={}; hierarchy rows={}; eligible metadata rows={}; effort values={}'.format(
                label, r['metadata_rows'], r['hierarchy_rows'], r['eligible_metadata_rows'], r['effort_values']))
        for c, values in relationships[task].items():
            lines.append('Container {}: dataset rows {}'.format(c, values))
    lines += ['\nCURRENT LOCAL SOURCE: '+source['status']]
    if source['status'] == 'PARSED_NOT_EXECUTED':
        lines.append('SHA256: '+source['sha256'])
        lines.append('Remaining Snowflake calls: '+str(len(source['snowflake_calls'])))
        for f in source['function_locations']:
            lines.append('  {}: lines {}-{}'.format(f['function'],f['start'],f['end']))
    lines += ['\nThis does not establish current API completeness or historical deletion dates.', 'Saved: '+str(output)]
    text = '\n'.join(lines)+'\n'
    (output/'summary.txt').write_text(text)
    print(text)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, InvalidOperation, UnicodeError, LookupError) as error:
        raise SystemExit('CHECK STOPPED: '+str(error))
