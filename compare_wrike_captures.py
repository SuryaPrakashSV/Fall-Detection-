#!/usr/bin/env python3
"""Offline comparison of a recorded CSV export and independent Wrike captures.

No network, tokens, source execution, or input edits. Writes a new comparison
folder. This produces review evidence, never an unconditional completeness claim.
Run from wrike-local-baseline with --case PATH. Exactly one successful export
receipt and one completed AFTER capture are selected automatically; ambiguity
requires explicit --receipt PATH and/or --after PATH. Blocked captures are kept
but excluded. An AFTER recovery is linked here without modifying its old receipt.
"""
import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, getcontext
import hashlib
import json
from pathlib import Path
import re
import sys

getcontext().prec = 60
csv.field_size_limit(16 * 1024 * 1024)
TASKS = ('parent_task_id', 'child_task_id', 'grandchild_task_id', 'baby_task_id',
         'grandbaby_task_id', 'great_grandbaby_task_id')
FOLDERS = ('parent_folder_id', 'child_folder_id', 'grandchild_folder_id',
           'baby_folder_id', 'grandbaby_folder_id')
RELATIONS = ('parentIds', 'superParentIds', 'superTaskIds', 'subTaskIds')
FIELDS = ('effortAllocation', *RELATIONS)
PROJECTION = ('id', 'scope', 'status', 'createdDate', 'updatedDate', *FIELDS)
NULLS = {'', 'null', 'none', 'nan', 'nat', '<na>', '\\n', 'placeholder'}
COMPLETE = {'CAPTURE_COMPLETE_NOT_COMPARED', 'CAPTURE_COMPLETE_REVIEW_REQUIRED'}
ID = re.compile(r'[A-Za-z0-9_-]+\Z')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def signature(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def folder_projection(rows):
    return sorted([{'id':r['id'], 'title':r.get('title'), 'scope':r.get('scope'),
                    'has_project':'project' in r, 'childIds':sorted(r.get('childIds', []))}
                   for r in rows], key=lambda r:r['id'])


def verify_folder_scope(rows, scope):
    projected = folder_projection(rows)
    raw = json.dumps(projected, sort_keys=True, separators=(',', ':')).encode()
    require(hashlib.sha256(raw).hexdigest() == scope['folder_structure_sha256'],
            'Saved folder scope disagrees with its raw inventory')
    mapping = {r['id']:r for r in projected}
    require(len(mapping) == len(projected), 'Duplicate raw folder IDs')
    seen, pending = set(), list(scope['root_ids'])
    while pending:
        fid = pending.pop()
        if fid in seen: continue
        require(fid in mapping, 'Folder closure has an unresolved ID')
        seen.add(fid); pending.extend(mapping[fid]['childIds'])
    require(seen == set(scope['folder_ids']), 'Saved folder membership disagrees with raw hierarchy')


def digest(path):
    first = path.stat()
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    last = path.stat()
    require((first.st_size, first.st_mtime_ns) == (last.st_size, last.st_mtime_ns),
            'Input changed while hashing: ' + str(path))
    return h.hexdigest()


def verify(path, expected):
    require(isinstance(expected, str) and digest(path) == expected,
            'Hash mismatch or missing recorded hash: ' + str(path))


def checked_files(directory, manifest, required):
    files = manifest.get('files', {})
    require(set(required) <= files.keys(), 'Missing required evidence hashes: ' + str(directory))
    for name, expected in files.items():
        path = (directory / name).resolve()
        require(directory.resolve() in path.parents, 'Evidence path escapes its directory')
        verify(path, expected)


def timestamp(value):
    d = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(d.tzinfo is not None, 'Timestamp lacks timezone')
    return d


def ident(value):
    value = (value or '').strip()
    return '' if value.lower() in NULLS else value


def number(value):
    text = str(value).strip()
    if len(text) > 100 or not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', text):
        return None
    try:
        n = Decimal(text)
        return n if n.is_finite() and n >= 0 and (not n or abs(n.adjusted()) <= 30) else None
    except InvalidOperation:
        return None


def effort(task):
    allocation = task.get('effortAllocation')
    if not isinstance(allocation, dict):
        return 'UNKNOWN', None
    if 'totalEffort' not in allocation:
        if allocation.get('mode') == 'None' and allocation.get('responsibleAllocation') == []:
            return 'NO_EFFORT_CONFIGURED', None
        return 'UNKNOWN', None
    n = number(allocation['totalEffort'])
    if n is None or (allocation.get('mode') == 'None' and n != 0):
        return 'UNKNOWN', None
    return ('EXPLICIT_ZERO' if n == 0 else 'EXPLICIT_POSITIVE'), n


def projection(task):
    require(isinstance(task, dict) and isinstance(task.get('id'), str)
            and ID.fullmatch(task['id']), 'Malformed task ID in reference')
    result = {k: task[k] for k in PROJECTION if k in task}
    for k in RELATIONS:
        if k in result:
            require(isinstance(result[k], list) and all(isinstance(x, str) and ID.fullmatch(x)
                    for x in result[k]), 'Malformed reference relationship')
            result[k] = sorted(result[k])
    return result


def load_reference(directory, phase, frozen, frozen_hash):
    manifest = read_json(directory / 'manifest.json')
    require(manifest.get('status') in COMPLETE and manifest.get('phase') == phase,
            'Reference is incomplete or has wrong phase: ' + str(directory))
    require(manifest.get('source_sha256') == frozen['source_sha256'] and
            manifest.get('frozen_scope_sha256') == frozen_hash and
            manifest.get('label') == frozen['label'] and manifest.get('space_id') == frozen['space_id'],
            'Reference provenance does not match preparation')
    checked_files(directory, manifest, ['summary.json', 'tasks.jsonl', 'scope_at_start.json', 'scope_at_end.json'])
    summary = read_json(directory / 'summary.json')
    roots = set(frozen['root_ids'])
    require(set(x['root_id'] for x in manifest['completed_roots']) == roots
            and len(manifest['completed_roots']) == len(roots), 'Reference did not complete every root exactly once')
    start, end = timestamp(manifest['started_utc']), timestamp(manifest['finished_utc'])
    require(start <= end, 'Reference has reversed capture times')
    records, expected = {}, Counter()
    with (directory / 'tasks.jsonl').open(encoding='utf-8') as f:
        for line in f:
            row = json.loads(line)
            tid = row['task_id']
            require(tid not in records and row['variants'], 'Duplicate/empty reference task record')
            values, hashes, folders, children, dates, observation_roots = [], set(), set(), set(), set(), set()
            issues = set()
            for variant in row['variants']:
                p = projection(variant['task'])
                require(p['id'] == tid and variant['observations'], 'Reference task/observation mismatch')
                h = signature(p)
                require(h not in hashes, 'Duplicate reference variant')
                hashes.add(h)
                values.append(effort(p))
                folders.update(p.get('parentIds', [])); folders.update(p.get('superParentIds', []))
                children.update(p.get('subTaskIds', []))
                dates.add(p.get('updatedDate', ''))
                if any(k not in p for k in RELATIONS): issues.add('MISSING_RELATIONSHIPS')
                if not p.get('createdDate') or not p.get('updatedDate'): issues.add('MISSING_TIMESTAMPS')
                if p.get('scope') != 'WsTask': issues.add('UNEXPECTED_TASK_SCOPE')
                for observation in variant['observations']:
                    root = observation['root_id']
                    observation_roots.add(root)
                    require(root in roots and start <= timestamp(observation['observed_utc']) <= end,
                            'Observation outside declared roots/time interval')
                    expected[(tid, root, observation['response_file'], h)] += 1
            require(observation_roots == set(row['root_ids']), 'Task root membership disagrees with observations')
            category, minutes = values[0] if len(values) == 1 else ('CONFLICTING_VARIANTS', None)
            records[tid] = dict(category=category, minutes=minutes, roots=observation_roots,
                                folders=folders, children=children, hashes=hashes, dates=dates, issues=issues)
    # Independently verify saved task records against raw successful pages and pagination.
    state, retry_requests, folder_bodies = {}, [], []
    seen_files = set()
    for request in manifest['requests']:
        filename = request.get('response_file')
        require(filename in manifest['files'] and filename not in seen_files, 'Missing/duplicate raw response evidence')
        seen_files.add(filename)
        verify(directory / filename, request.get('sha256'))
        require(start <= timestamp(request['started_utc']) <= timestamp(request['finished_utc']) <= end,
                'Request times outside capture interval')
        endpoint = request['endpoint']
        if request.get('status') != 200:
            retry_requests.append({'endpoint': endpoint, 'status': request.get('status')})
            continue
        body = read_json(directory / filename)
        if endpoint == 'spaces/' + frozen['space_id'] + '/folders':
            require(body.get('kind') == 'folderTree', 'Invalid folder response kind')
            require(isinstance(body.get('data'), list) and not body.get('nextPageToken'),
                    'Invalid folder inventory')
            folder_bodies.append(body['data'])
            continue
        match = re.fullmatch(r'folders/([^/]+)/tasks', endpoint)
        require(match is not None and match[1] in roots, 'Unexpected reference endpoint')
        root = match[1]
        params = request.get('params', {})
        require(params.get('descendants') == 'true' and params.get('subTasks') == 'true'
                and params.get('pageSize') == 1000 and set(json.loads(params.get('fields', '[]'))) == set(FIELDS)
                and set(params) <= {'descendants', 'subTasks', 'pageSize', 'fields', 'nextPageToken'},
                'Reference query differs from the agreed task scope')
        previous = state.get(root)
        require(previous is None or previous['next'], 'Additional page after terminal page')
        require(params.get('nextPageToken') == (previous['next'] if previous else None), 'Pagination chain mismatch')
        require(body.get('kind') == 'tasks' and isinstance(body.get('data'), list)
                and len(body['data']) <= 1000, 'Invalid raw task page')
        following = body.get('nextPageToken')
        tokens = previous['tokens'] if previous else set()
        if following is not None:
            require(isinstance(following, str) and following and body['data'] and following not in tokens,
                    'Empty page or repeated/invalid pagination token')
            tokens.add(following)
        for task in body['data']:
            p = projection(task)
            key = (p['id'], root, filename, signature(p))
            require(expected[key] > 0, 'Raw API task is missing or differs in tasks.jsonl')
            expected[key] -= 1
            if not expected[key]: del expected[key]
        state[root] = {'next': following, 'tokens': tokens,
                       'pages': (previous['pages'] if previous else 0) + 1,
                       'observations': (previous['observations'] if previous else 0) + len(body['data'])}
    require(not expected and set(state) == roots and all(x['next'] is None for x in state.values()),
            'Incomplete raw task/reference reconciliation')
    require(len(folder_bodies) == 2, 'Reference must include beginning and ending folder inventories')
    for rows, name in zip(folder_bodies, ('scope_at_start.json','scope_at_end.json')):
        verify_folder_scope(rows, read_json(directory / name))
    for item in manifest['completed_roots']:
        s = state[item['root_id']]
        require(item['pages'] == s['pages'] and item['task_observations'] == s['observations'],
                'Completed-root counters differ from raw pages')
    require(len(records) == summary.get('unique_tasks') and records, 'Reference task count mismatch or empty reference')
    return records, manifest, summary, retry_requests


def read_export(path, row_writer):
    tasks, counts, refresh, bad_keys, folder_keys = {}, Counter(), Counter(), set(), set()
    with path.open(newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f, strict=True)
        headers = [x.strip() for x in (reader.fieldnames or [])]
        require(headers and len(headers) == len(set(headers)) and '' not in headers,
                'Duplicate or empty CSV headers')
        require({'id', 'key', 'effortAllocation_totalEffort', *TASKS, *FOLDERS} <= set(headers),
                'Required CSV columns are missing')
        reader.fieldnames = headers
        for n, row in enumerate(reader, 1):
            require(None not in row and all(v is not None for v in row.values()), 'Malformed CSV row')
            counts['physical_rows'] += 1
            refresh[row.get('Data refresh', '')] += 1
            key, container = ident(row['key']), ident(row['id'])
            folders = {ident(row[c]) for c in FOLDERS} | {container}
            folders.discard('')
            hierarchy = [ident(row[c]) for c in TASKS]
            deepest = next((x for x in reversed(hierarchy) if x), '')
            folder_own = key and key in folders
            if folder_own and key not in hierarchy:
                counts['folder_own_rows_excluded'] += 1; folder_keys.add(key)
                continue
            if folder_own or not key or not deepest or key != deepest:
                counts['ambiguous_identity_rows'] += 1; bad_keys.add(key)
                row_writer.writerow([n, 'AMBIGUOUS_TASK_IDENTITY', key, deepest])
                continue
            task = tasks.setdefault(key, dict(values=set(), issues=set(), folders=set(), dates=set(), rows=0))
            task['rows'] += 1; task['folders'].update(folders)
            task['dates'].add(row.get('updatedDate', '').strip())
            minutes = number(row['effortAllocation_totalEffort'])
            if minutes is None:
                task['issues'].add('NULL_OR_INVALID_EFFORT')
                row_writer.writerow([n, 'NULL_OR_INVALID_EFFORT', key, deepest])
            else: task['values'].add(minutes)
    for key, task in tasks.items():
        if key in bad_keys or key in folder_keys: task['issues'].add('AMBIGUOUS_TASK_IDENTITY')
        if len(task['values']) != 1: task['issues'].add('CONFLICTING_OR_UNKNOWN_EFFORT')
        task['minutes'] = next(iter(task['values'])) if len(task['values']) == 1 and not task['issues'] else None
    require(counts['physical_rows'] > 0, 'Empty CSV')
    return tasks, dict(counts), dict(refresh)


def pair(csv_task, ref):
    if csv_task is None: return 'REFERENCE_ONLY' if ref else 'ABSENT_BOTH'
    if ref is None: return 'CSV_ONLY'
    if csv_task['issues']: return 'CSV_VALUE_REQUIRES_REVIEW'
    if ref['issues'] or ref['category'] in {'UNKNOWN', 'CONFLICTING_VARIANTS'}:
        return 'REFERENCE_VALUE_REQUIRES_REVIEW'
    if ref['category'] == 'NO_EFFORT_CONFIGURED':
        return 'CSV_ZERO_REFERENCE_NO_EFFORT' if csv_task['minutes'] == 0 else 'CSV_POSITIVE_REFERENCE_NO_EFFORT'
    return 'MATCH_NUMERIC' if csv_task['minutes'] == ref['minutes'] else 'EFFORT_MISMATCH'


def numeric_summary(records):
    values = [v['minutes'] for v in records.values() if v['minutes'] is not None]
    return {'unique_tasks': len(records), 'explicit_numeric_tasks': len(values),
            'unknown_or_nonnumeric_tasks': len(records) - len(values),
            'explicit_numeric_hours': str(sum(values, Decimal(0)) / 60),
            'not_a_validated_complete_total': True}


def choose(paths, description):
    require(len(paths) == 1, f'Expected exactly one {description}; found {len(paths)}. Select it explicitly.')
    return paths[0]


def text(value):
    return '' if value is None else str(value)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--case', type=Path, required=True)
    parser.add_argument('--receipt', type=Path)
    parser.add_argument('--after', type=Path)
    args = parser.parse_args()
    case = args.case.resolve()
    prep = read_json(case / 'manifest.json')
    require(prep.get('status') == 'PREPARED_NOT_VALIDATED', 'Preparation incomplete')
    checked_files(case, prep, ['frozen_scope.json', 'folder_tree.json', 'comparison_specification.json'])
    frozen = read_json(case / 'frozen_scope.json'); frozen_hash = digest(case / 'frozen_scope.json')
    verify_folder_scope(read_json(case / 'folder_tree.json')['data'], frozen)
    receipt_path = args.receipt or choose([p for p in case.glob('extraction_*/receipt.json')
        if read_json(p).get('extractor_exit_code') == 0 and read_json(p).get('csv', {}).get('new_file_observed')], 'completed extraction receipt')
    receipt_path = receipt_path.resolve(); receipt = read_json(receipt_path)
    require(receipt_path.parent.parent == case, 'Receipt is outside this comparison case')
    require(receipt.get('extractor_exit_code') == 0 and receipt.get('csv', {}).get('new_file_observed') is True,
            'No successful fresh CSV export in receipt')
    require(receipt.get('source_sha256') == frozen['source_sha256'] and
            receipt.get('frozen_scope_sha256') == frozen_hash and receipt.get('label') == frozen['label'],
            'Extraction receipt provenance mismatch')
    source = Path(receipt['source_path']); verify(source, receipt['source_sha256'])
    csv_path = Path(receipt['csv']['path']); verify(csv_path, receipt['csv']['sha256'])
    log_path = receipt_path.parent / 'extractor.log'; verify(log_path, receipt['extractor_log_sha256'])
    before_path = Path(receipt['before_capture_path']).resolve()
    require(before_path.parent == case, 'BEFORE reference outside this case')
    verify(before_path / 'manifest.json', receipt['before_manifest_sha256'])
    after_path = (args.after or choose([p for p in case.glob('reference_after_*')
        if (p / 'manifest.json').is_file() and read_json(p / 'manifest.json').get('status') in COMPLETE], 'completed AFTER capture')).resolve()
    require(after_path.parent == case, 'AFTER reference outside this case')
    print('Checking BEFORE evidence and raw API pages...', flush=True)
    before, bm, bs, br = load_reference(before_path, 'before', frozen, frozen_hash)
    print('Checking AFTER evidence and raw API pages...', flush=True)
    after, am, ass, ar = load_reference(after_path, 'after', frozen, frozen_hash)
    b_end, x_start, x_end, a_start = map(timestamp, [bm['finished_utc'], receipt['started_utc'], receipt['finished_utc'], am['started_utc']])
    require(b_end <= x_start <= x_end <= a_start, 'Capture/extraction times do not bracket the new export')
    require(receipt.get('before_finished_utc') == bm['finished_utc'], 'Receipt BEFORE finish time mismatch')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    out = case / ('offline_comparison_' + stamp); out.mkdir(exist_ok=False)
    initial = {'status':'COMPARISON_RUNNING', 'validation_complete':False}
    write_json(out / 'summary.json', initial)
    try:
        with (out / 'csv_row_issues.csv').open('w', newline='') as f:
            writer = csv.writer(f); writer.writerow(['data_row_number','issue','key','deepest_task_id'])
            exported, row_counts, refresh = read_export(csv_path, writer)
        require(row_counts['physical_rows'] == receipt['csv']['physical_rows'], 'CSV row count differs from receipt')
        print('Comparing task IDs, effort and folder membership...', flush=True)
        scopes = [read_json(p / n) for p in (before_path, after_path)
                  for n in ('scope_at_start.json','scope_at_end.json')]
        require(all(set(s['root_ids']) == set(frozen['root_ids']) for s in scopes), 'Root IDs changed')
        folder_drift = any(s['folder_structure_sha256'] != frozen['folder_structure_sha256']
                          or set(s['folder_ids']) != set(frozen['folder_ids']) for s in scopes)
        added = set().union(*(set(s['folder_ids']) - set(frozen['folder_ids']) for s in scopes))
        removed = set().union(*(set(frozen['folder_ids']) - set(s['folder_ids']) for s in scopes))
        counts, flags_count = Counter(), Counter()
        reference_categories = {'before':dict(Counter(t['category'] for t in before.values())),
                                'after':dict(Counter(t['category'] for t in after.values()))}
        reasons = set(bs.get('review_reasons', []) + ass.get('review_reasons', []))
        if folder_drift: reasons.add('FOLDER_SCOPE_CHANGED')
        if br or ar: reasons.add('RECOVERED_REFERENCE_REQUESTS_REQUIRE_REVIEW')
        if row_counts.get('ambiguous_identity_rows'): reasons.add('AMBIGUOUS_CSV_ROWS')
        roots = set(frozen['root_ids'])
        changed = added | removed
        folder_totals = {fid:{'csv':set(), 'before':set(), 'after':set()} for fid in changed}
        columns = ['task_id','before_comparison','after_comparison','review_flags','csv_minutes',
                   'before_category','before_minutes','after_category','after_minutes',
                   'csv_roots','before_roots','after_roots','csv_updated_dates','before_updated_dates',
                   'after_updated_dates','changed_scope_folder_ids']
        issue_tasks = 0
        with (out / 'task_comparison.csv').open('w', newline='') as all_f, (out / 'task_discrepancies.csv').open('w', newline='') as issue_f:
            all_w, issue_w = csv.writer(all_f), csv.writer(issue_f)
            all_w.writerow(columns); issue_w.writerow(columns)
            for tid in sorted(exported.keys() | before.keys() | after.keys()):
                c, b, a = exported.get(tid), before.get(tid), after.get(tid)
                left, right = pair(c,b), pair(c,a)
                counts[left + ' / ' + right] += 1
                flags = set()
                for label, result in [('BEFORE',left),('AFTER',right)]:
                    if result not in {'MATCH_NUMERIC','CSV_ZERO_REFERENCE_NO_EFFORT','ABSENT_BOTH'}:
                        flags.add(label + '_' + result)
                if b and a and b['hashes'] != a['hashes']: flags.add('REFERENCE_TASK_CHANGED')
                if b and a and b['roots'] != a['roots']: flags.add('REFERENCE_ROOT_MEMBERSHIP_CHANGED')
                c_roots = (c['folders'] & roots) if c else set()
                if c and b and c_roots != b['roots']: flags.add('CSV_BEFORE_ROOT_MEMBERSHIP_DIFFERS')
                if c and a and c_roots != a['roots']: flags.add('CSV_AFTER_ROOT_MEMBERSHIP_DIFFERS')
                touched = set()
                for label, record in [('csv',c),('before',b),('after',a)]:
                    if record:
                        for fid in record['folders'] & changed:
                            folder_totals[fid][label].add(tid); touched.add(fid)
                if touched: flags.add('LINKED_TO_CHANGED_SCOPE_FOLDER')
                if 'CSV_ZERO_REFERENCE_NO_EFFORT' in (left,right):
                    reasons.add('CSV_ZERO_VS_NO_EFFORT_REPRESENTATION_REQUIRES_REVIEW')
                if flags: issue_tasks += 1
                flags_count.update(flags)
                row = [tid,left,right,';'.join(sorted(flags)),text(c['minutes']) if c else '',
                       b['category'] if b else 'ABSENT',text(b['minutes']) if b else '',
                       a['category'] if a else 'ABSENT',text(a['minutes']) if a else '',
                       ';'.join(sorted(c_roots)), ';'.join(sorted(b['roots'])) if b else '',
                       ';'.join(sorted(a['roots'])) if a else '', ';'.join(sorted(c['dates'])) if c else '',
                       ';'.join(sorted(b['dates'])) if b else '', ';'.join(sorted(a['dates'])) if a else '',
                       ';'.join(sorted(touched))]
                all_w.writerow(row)
                if flags: issue_w.writerow(row)
        if issue_tasks: reasons.add('TASK_DISCREPANCIES_REQUIRE_EVIDENCE_REVIEW')
        log_count = 0
        with (out / 'extraction_log_review.csv').open('w', newline='') as f:
            writer = csv.writer(f); writer.writerow(['line_number','matched_terms'])
            pattern = re.compile(r'\b(error|failed|failure|exception|traceback|missing|retry|retries|429|400|401|403|404|500|502|503|504)\b', re.I)
            with log_path.open(encoding='utf-8', errors='replace') as log:
                for n, line in enumerate(log, 1):
                    terms = sorted({x.lower() for x in pattern.findall(line)})
                    if terms: writer.writerow([n,';'.join(terms)]); log_count += 1
        if log_count: reasons.add('EXTRACTION_LOG_CANDIDATE_LINES_REQUIRE_REVIEW')
        require(digest(csv_path) == receipt['csv']['sha256'], 'CSV changed while comparing')
        folder_results = {}
        for fid, sets in folder_totals.items():
            folder_results[fid] = {label:numeric_summary({t:dataset[t] for t in sets[label]})
                for label,dataset in [('csv',exported),('before',before),('after',after)]}
        result = {
            'status':'COMPARISON_COMPLETE_REVIEW_REQUIRED' if reasons else 'OBSERVED_TASK_EFFORT_MATCH',
            'validation_complete':False, 'review_reasons':sorted(reasons),
            'row_counts':row_counts, 'csv_refresh_values':refresh,
            'totals':{'csv':numeric_summary(exported),'before':numeric_summary(before),'after':numeric_summary(after)},
            'reference_effort_categories':reference_categories, 'comparison_counts':dict(counts),
            'review_flag_counts':dict(flags_count), 'task_discrepancy_count':issue_tasks,
            'log_candidate_line_count':log_count, 'added_scope_folder_ids':sorted(added),
            'removed_scope_folder_ids':sorted(removed), 'changed_folder_task_totals':folder_results,
            'reference_request_retries':{'before':br,'after':ar},
            'timing':{'before_started_utc':bm['started_utc'],'before_finished_utc':bm['finished_utc'],
                'extractor_started_utc':receipt['started_utc'],'extractor_finished_utc':receipt['finished_utc'],
                'after_started_utc':am['started_utc'],'after_finished_utc':am['finished_utc'],
                'before_to_extraction_seconds':(x_start-b_end).total_seconds(),
                'extraction_elapsed_seconds':(x_end-x_start).total_seconds(),
                'extraction_to_after_seconds':(a_start-x_end).total_seconds()},
            'provenance':{'receipt_path':str(receipt_path),'receipt_sha256':digest(receipt_path),
                'csv_path':str(csv_path),'csv_sha256':receipt['csv']['sha256'],
                'before_path':str(before_path),'before_manifest_sha256':digest(before_path/'manifest.json'),
                'after_path':str(after_path),'after_manifest_sha256':digest(after_path/'manifest.json'),
                'after_selected_separately_from_launcher':str(after_path) != receipt.get('after_capture_path'),
                'source_sha256':frozen['source_sha256'],'comparator_sha256':digest(Path(__file__))},
            'limitations':[
                'Results describe the observed token, root scope and capture window, not unseen account-wide tasks.',
                'Matching before/after observations cannot rule out intervening changes or pagination races.',
                'Updated timestamps alone do not prove the cause of an omission or effort difference.',
                'Folder membership differences must be investigated before a strict scope-equivalence claim.',
                'Numeric totals exclude unknown/no-effort reference values; those remain separately classified.',
                'Log keyword matches are review candidates, not confirmed failures; absence is not a request-completeness proof.',
                'Historical deletion dates and the two September 24 omissions are not resolved by this comparison.']}
        write_json(out/'summary.json',result)
        write_json(out/'output_hashes.json',{p.name:digest(p) for p in sorted(out.iterdir()) if p.is_file()})
        print('\nStatus:',result['status'])
        print('Unique tasks: CSV={}, BEFORE={}, AFTER={}'.format(len(exported),len(before),len(after)))
        print('Task discrepancies requiring review:',issue_tasks)
        print('Log candidate lines requiring review:',log_count)
        print('Review reasons:', '; '.join(sorted(reasons)) or 'none detected')
        print('Output:',out)
        print('Share summary.json first. No API calls or input edits were made.')
        return 3 if reasons else 0
    except Exception as exc:
        write_json(out/'summary.json',{'status':'COMPARISON_BLOCKED','validation_complete':False,
                                     'reason':str(exc),'partial_outputs_not_validated':True})
        raise


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError, csv.Error) as exc:
        print('Comparison stopped:',str(exc))
        raise SystemExit(2)
