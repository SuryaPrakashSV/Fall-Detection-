#!/usr/bin/env python3
"""Review existing effort reconciliation outputs offline. Standard library only.

Run beside calculate_wrike_effort.py:
    python3 review_wrike_effort_gaps.py
Or select a report explicitly with --report /path/to/timestamped/report.
Reads saved files, never requests a token, never calls an API, never edits inputs.
"""
import argparse
import csv
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path

getcontext().prec = 60
csv.field_size_limit(16 * 1024 * 1024)
ZERO = Decimal(0)
TOLERANCE = Decimal('0.000001')  # CSV hours were rounded to ten decimal places.
OLD = 'saved_surya_vs_saved_akash'
NEW = 'current_snowflake_vs_saved_akash'
KINDS = ('BASELINE_ONLY_OUTSIDE_56', 'AKASH_ONLY',
         'SHARED_EFFORT_CHANGED', 'SHARED_EFFORT_UNKNOWN')
FOLDERS = ('parent_folder_id', 'child_folder_id', 'grandchild_folder_id',
           'baby_folder_id', 'grandbaby_folder_id')
TASKS = ('parent_task_id', 'child_task_id', 'grandchild_task_id',
         'baby_task_id', 'grandbaby_task_id', 'great_grandbaby_task_id')
FOUR = {'IEADQP2VI5CGZ6N7': 'Intake Form - Resourcing Template',
        'IEADQP2VI5CGZFX4': 'NPI Project Blueprints',
        'IEADQP2VI5IWLCDD': 'Test Import',
        'IEADQP2VI5DX7CRB': 'xxOLD Blueprints'}
NULLS = {'', 'nan', 'none', 'null', 'nat', '<na>', 'placeholder'}


def identity(value):
    value = (value or '').strip()
    return '' if value.lower() in NULLS else value


def number(value):
    if value is None or str(value).strip() == '':
        return None
    try:
        value = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('Invalid numeric value: ' + repr(value))
    if not value.is_finite():
        raise ValueError('Non-finite number')
    return value


def exact(value):
    return '' if value is None else format(value, 'f')


def display(value):
    return 'UNKNOWN' if value is None else format(value, ',.2f')


def total(rows, key='_delta'):
    values = [r[key] for r in rows]
    return None if any(v is None for v in values) else sum(values, ZERO)


def same(actual, expected, label):
    expected = number(expected)
    if (actual is None) != (expected is None) or (
            actual is not None and abs(actual - expected) > TOLERANCE):
        raise ValueError(f'{label}: calculated {actual}, summary says {expected}')


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for part in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(part)
    return digest.hexdigest()


def rows_from(path):
    with path.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle, strict=True)
        headers = reader.fieldnames or []
        if not headers or len(headers) != len(set(headers)) or '' in headers:
            raise ValueError(f'{path.name}: invalid CSV headers')
        for row in reader:
            if None in row or any(v is None for v in row.values()):
                raise ValueError(f'{path.name}: malformed row near {reader.line_num}')
            yield row


def json_ids(value, label):
    result = json.loads(value)
    if not isinstance(result, list) or any(not isinstance(v, str) or not identity(v) for v in result):
        raise ValueError('Expected JSON ID list: ' + label)
    if len(result) != len(set(result)):
        raise ValueError('Duplicate IDs in ' + label)
    return tuple(sorted(result))


def read_differences(path, comparison):
    before = sha(path)
    output, seen = [], set()
    for row in rows_from(path):
        task = identity(row.get('task_id'))
        kind = row['difference_type']
        if not task or task in seen or kind not in KINDS:
            raise ValueError(f'{path.name}: invalid or repeated task ID / difference type')
        seen.add(task)
        a, b, delta = (number(row[k]) for k in (
            'baseline_effort_hours', 'akash_effort_hours', 'baseline_minus_akash_hours'))
        if any(v is not None and v < 0 for v in (a, b)):
            raise ValueError('Negative source effort for ' + task)
        if kind == 'BASELINE_ONLY_OUTSIDE_56':
            if b is not None:
                raise ValueError('Unexpected Akash effort for baseline-only task ' + task)
            expected = a
        elif kind == 'AKASH_ONLY':
            if a is not None:
                raise ValueError('Unexpected baseline effort for Akash-only task ' + task)
            expected = -b if b is not None else None
        else:
            expected = a - b if a is not None and b is not None else None
            if kind == 'SHARED_EFFORT_CHANGED' and (expected is None or expected == 0):
                raise ValueError('Invalid changed-effort row ' + task)
            if kind == 'SHARED_EFFORT_UNKNOWN' and expected is not None:
                raise ValueError('Unexpected known effort in unknown row ' + task)
        same(delta, expected, task + ' delta')
        row.update(_a=a, _b=b, _delta=delta)
        for side in ('baseline', 'akash'):
            for field in ('direct_container_ids', 'excluded_item_scope_ids', 'missing_56_project_ids'):
                key = side + '_' + field
                row['_' + key] = json_ids(row[key + '_json'], key)
            titles = json.loads(row[side + '_titles_json'])
            if not isinstance(titles, list) or any(not isinstance(v, str) for v in titles):
                raise ValueError('Invalid title metadata for ' + task)
            row['_' + side + '_titles'] = titles
        if kind == 'BASELINE_ONLY_OUTSIDE_56' and row['_baseline_missing_56_project_ids']:
            raise ValueError('Outside-56 row has a missing-project membership: ' + task)
        output.append(row)
    counts = Counter(r['difference_type'] for r in output)
    for kind in KINDS:
        if counts[kind] != comparison['task_difference_counts'].get(kind, 0):
            raise ValueError(path.name + ': task counts differ from saved summary for ' + kind)
    parts = comparison['partitions']
    for kind, term, key in (('BASELINE_ONLY_OUTSIDE_56', 'baseline_only_outside_56', '_a'),
                            ('AKASH_ONLY', 'akash_only', '_b')):
        subset = [r for r in output if r['difference_type'] == kind]
        if len(subset) != parts[term]['task_count']:
            raise ValueError('Partition count mismatch: ' + term)
        same(total(subset, key), parts[term]['hours'], term)
    shared = [r for r in output if r['difference_type'].startswith('SHARED_')]
    same(total(shared), parts['shared_task_effort_delta']['hours'], 'shared effort delta')
    if sha(path) != before:
        raise ValueError('Difference file changed during read')
    return output, {'path': str(path), 'sha256': before, 'rows': len(output)}


def source_names(root, audit, wanted):
    """Names are optional; only use source bytes matching the saved SHA256."""
    original = Path(audit['path'])
    candidates = [original]
    parts = original.parts
    if 'output' in parts:
        candidates.append(root.joinpath(*parts[parts.index('output'):]))
    candidates.append(root / original.name)
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        return {}, 'Source unavailable; grouping uses IDs.'
    if sha(path) != audit['sha256']:
        return {}, 'Source hash changed; names not used. Saved difference rows remain the input.'
    names = defaultdict(set)
    for row in rows_from(path):
        key = identity(row.get('key'))
        if key not in wanted:
            continue
        folders = {identity(row.get(c)) for c in FOLDERS}
        tasks = {identity(row.get(c)) for c in TASKS}
        if key not in tasks and (key == identity(row.get('id')) or key in folders):
            title = identity(row.get('title'))
            if title:
                names[key].add(title)
    if sha(path) != audit['sha256']:
        raise ValueError('Source changed during name lookup: ' + str(path))
    return {k: sorted(v) for k, v in names.items()}, 'Source SHA256 matched; own-row names read.'


def sorted_rows(rows):
    return sorted(rows, key=lambda r: (r['_delta'] is None,
                                      -abs(r['_delta'] or ZERO), r['task_id']))


def scope_label(row):
    side = 'akash' if row['difference_type'] == 'AKASH_ONLY' else 'baseline'
    scopes = set(row['_' + side + '_excluded_item_scope_ids'])
    if scopes & FOUR.keys():
        return 'WITHIN_ONE_OR_MORE_CONFIRMED_FOLDERS'
    return 'WITHIN_OTHER_EXCLUDED_SCOPE' if scopes else 'OUTSIDE_FIVE_EXCLUDED_SCOPES'


def group_rows(rows, names, source):
    buckets = defaultdict(list)
    for row in rows:
        key = (row['difference_type'], row['_baseline_direct_container_ids'],
               row['_akash_direct_container_ids'], scope_label(row))
        buckets[key].append(row)
    result = []
    for (kind, a, b, scope), values in buckets.items():
        def labels(ids, dataset):
            return {i: names.get(dataset, {}).get(i, []) for i in ids}
        ranked = sorted_rows(values)
        result.append({'difference_type': kind, 'scope_category': scope,
            'task_count': len(values), 'baseline_minus_akash_hours': exact(total(values)),
            'baseline_container_ids_json': json.dumps(a),
            'akash_container_ids_json': json.dumps(b),
            'baseline_container_names_json': json.dumps(labels(a, source), ensure_ascii=False),
            'akash_container_names_json': json.dumps(labels(b, 'akash'), ensure_ascii=False),
            'example_task_ids_json': json.dumps([r['task_id'] for r in ranked[:3]])})
    return sorted(result, key=lambda r: (r['difference_type'],
        r['baseline_minus_akash_hours'] == '',
        -abs(number(r['baseline_minus_akash_hours']) or ZERO),
        r['baseline_container_ids_json'], r['akash_container_ids_json']))


def write_csv(path, rows, fields):
    with path.open('x', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def run(root, report=None):
    if report is None:
        candidates = list((root / 'output' / 'effort_reconciliation').glob('*/effort_reconciliation_details.json'))
        if not candidates:
            raise ValueError('No saved effort reports found. Save this script in wrike-local-baseline, '
                             'or use --report with the existing report folder.')
        # Existing calculator uses fixed-width UTC timestamp directory names.
        report = max(candidates, key=lambda p: p.parent.name).parent
    details_path = report / 'effort_reconciliation_details.json'
    details_hash = sha(details_path)
    details = json.loads(details_path.read_text(encoding='utf-8-sig'))
    if details.get('confirmed_project_count') != 56:
        raise ValueError('Expected the saved 56-project report')
    comparisons = details['comparisons']
    if any(name not in comparisons for name in (OLD, NEW)):
        raise ValueError('Selected report does not contain both comparisons. Use --report with the complete report folder.')
    sources = {OLD: 'surya', NEW: 'snowflake'}
    data, audits = {}, {}
    for name in (OLD, NEW):
        filename = comparisons[name]['task_difference_file']
        if Path(filename).name != filename:
            raise ValueError('Expected a difference filename within the report folder')
        data[name], audits[name] = read_differences(report / filename, comparisons[name])
    if sha(details_path) != details_hash:
        raise ValueError('Saved details changed during read')
    print('Both difference files match saved counts and effort totals.', flush=True)
    names, name_notes = {}, {}
    for dataset in ('surya', 'snowflake', 'akash'):
        side = 'akash' if dataset == 'akash' else 'baseline'
        selected = [name for name in (OLD, NEW) if dataset == 'akash' or sources[name] == dataset]
        wanted = {i for name in selected for row in data[name]
                  for i in row['_' + side + '_direct_container_ids']}
        print('Reading saved container names: ' + dataset, flush=True)
        names[dataset], name_notes[dataset] = source_names(root, details['datasets'][dataset]['input'], wanted)
    out = report / ('gap_review_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ'))
    out.mkdir(parents=True, exist_ok=False)
    text = ['WRIKE EFFORT GAP REVIEW', 'Input report: ' + str(report),
            'Saved counts and effort totals: MATCH',
            'Metric: recorded planned task effort in hours. No live API check.',
            'All task statuses included. Differences alone do not prove an access problem.', '']
    for dataset in ('surya', 'akash', 'snowflake'):
        audit = details['datasets'][dataset]['input']
        text += [dataset + ' refresh: ' + json.dumps(audit.get('data_refresh_counts', {})),
                 '  ' + name_notes[dataset]]
    group_fields = ('difference_type', 'scope_category', 'task_count', 'baseline_minus_akash_hours',
        'baseline_container_ids_json', 'akash_container_ids_json', 'baseline_container_names_json',
        'akash_container_names_json', 'example_task_ids_json')
    for name in (OLD, NEW):
        rows = data[name]
        groups = group_rows(rows, names, sources[name])
        write_csv(out / ('container_groups_' + name + '.csv'), groups, group_fields)
        text += ['', name.upper(), f'Task difference rows: {len(rows):,}',
                 'Baseline minus (Akash + 56-project union): ' +
                 display(number(comparisons[name]['baseline_minus_Akash_minus_56_hours'])) + ' h',
                 'Counts and signed hours by difference type:']
        for kind in KINDS:
            selected = [r for r in rows if r['difference_type'] == kind]
            text.append(f'  {kind}: {len(selected):,} tasks; {display(total(selected))} h')
        text.append('Exclusive scope categories (baseline side, except Akash-only uses Akash side):')
        for scope in ('WITHIN_ONE_OR_MORE_CONFIRMED_FOLDERS', 'WITHIN_OTHER_EXCLUDED_SCOPE',
                      'OUTSIDE_FIVE_EXCLUDED_SCOPES'):
            selected = [r for r in rows if scope_label(r) == scope]
            text.append(f'  {scope}: {len(selected):,} tasks; {display(total(selected))} h')
        text.append('Largest container groups in each difference type:')
        for kind in KINDS:
            for group in [g for g in groups if g['difference_type'] == kind][:3]:
                text.append(f'  {kind}: {group["task_count"]} tasks; '
                            f'{display(number(group["baseline_minus_akash_hours"]))} h')
                for label in ('baseline', 'akash'):
                    ids = json.loads(group[label + '_container_ids_json'])
                    labels = json.loads(group[label + '_container_names_json'])
                    if ids:
                        text.append('    ' + label + ': ' + '; '.join(
                            i + ' (' + ' / '.join(labels[i]) + ')' if labels[i] else i + ' (name unavailable)'
                            for i in ids))
                text.append('    ' + group['scope_category'])
        text.append('Largest individual tasks per difference type:')
        for kind in KINDS:
            for row in sorted_rows([r for r in rows if r['difference_type'] == kind])[:2]:
                title = ' / '.join(row['_baseline_titles'] or row['_akash_titles']) or '(title unavailable)'
                text.append(f'  {kind}: {row["task_id"]}; {display(row["_delta"])} h; {title}')
    old_map = {r['task_id']: r for r in data[OLD]}
    new_map = {r['task_id']: r for r in data[NEW]}
    transitions = []
    buckets = defaultdict(list)
    for task in sorted(old_map.keys() | new_map.keys()):
        old, new = old_map.get(task), new_map.get(task)
        old_type = old['difference_type'] if old else 'NOT_IN_OLD_EXCEPTION_FILE'
        new_type = new['difference_type'] if new else 'NOT_IN_NEW_EXCEPTION_FILE'
        # Absent exceptions contribute zero to exception totals, not zero source effort.
        a, b = old['_delta'] if old else ZERO, new['_delta'] if new else ZERO
        row = {'task_id': task, 'old_difference_type': old_type, 'new_difference_type': new_type,
               'old_exception_delta_hours': exact(a), 'new_exception_delta_hours': exact(b),
               'change_in_exception_delta_hours': exact(b - a if a is not None and b is not None else None)}
        transitions.append(row)
        buckets[(old_type, new_type)].append(row)
    write_csv(out / 'exception_changes_between_comparisons.csv', transitions,
              ('task_id', 'old_difference_type', 'new_difference_type', 'old_exception_delta_hours',
               'new_exception_delta_hours', 'change_in_exception_delta_hours'))
    text += ['', 'EXCEPTIONS ACROSS THE TWO COMPARISONS',
             'Absent from an exception file does not mean absent from the full source.',
             'Both comparisons use the same saved Akash export.']
    for (a, b), values in sorted(buckets.items()):
        amounts = [number(v['change_in_exception_delta_hours']) for v in values]
        change = None if None in amounts else sum(amounts, ZERO)
        text.append(f'  {a} -> {b}: {len(values):,} tasks; exception delta change {display(change)} h')
    text += ['', 'INTERPRETATION',
             'Container groups preserve each full container-ID set. A task appears in one group per comparison.',
             'Groups can include folders or multiple containers; they are not assumed to be individual projects.',
             'Folder-scope membership identifies where a task occurs, not why it was missing.',
             'Changed shared-task effort is observed between snapshots; the change reason is not established.',
             'Inspect the largest persistent groups first. Check associated extraction errors or targeted live IDs only if needed.',
             'Final completeness and access-cause validation remain open.',
             '', 'Share this file: ' + str(out / 'gap_review_summary.txt')]
    (out / 'gap_review_summary.txt').write_text('\n'.join(text) + '\n', encoding='utf-8')
    (out / 'review_inputs.json').write_text(json.dumps({
        'details_file': str(details_path), 'details_sha256': details_hash,
        'difference_files': audits, 'source_name_checks': name_notes}, indent=2) + '\n', encoding='utf-8')
    print('\n'.join(text))
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, help='Existing timestamped effort report folder')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    run(root, args.report.expanduser().resolve() if args.report else None)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, UnicodeError, csv.Error) as error:
        print('STOPPED: ' + str(error), file=sys.stderr)
        sys.exit(1)
