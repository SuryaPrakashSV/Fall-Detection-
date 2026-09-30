#!/usr/bin/env python3
"""Read-only current effort capture for the five API-only projects.

Run beside wrike_report_analysis.py and snowflake_snapshot.csv on the office Mac.
Uses only GET /folders/{id} and GET /folders/{id}/tasks for verified project IDs.
This is a later live supplement; it does not change the frozen Snowflake total.
"""
import argparse
import ast
import csv
import getpass
import hashlib
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, getcontext
from email.utils import parsedate_to_datetime
from pathlib import Path

getcontext().prec = 60
FIELDS = ['effortAllocation', 'parentIds', 'superParentIds', 'superTaskIds', 'subTaskIds']
EXPECTED_PROJECTS = 5


def now():
    return datetime.now(timezone.utc).isoformat()


def check(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def load(path):
    with Path(path).open(encoding='utf-8-sig') as stream:
        return json.load(stream)


def save(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, default=str)


def rows(path):
    csv.field_size_limit(sys.maxsize)
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        check(reader.fieldnames and len(reader.fieldnames) == len(set(reader.fieldnames)), 'Invalid CSV headers')
        for row in reader:
            check(None not in row and all(v is not None for v in row.values()), 'Malformed CSV row')
            yield row


def csv_write(path, data, fields):
    with Path(path).open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(data)


def verify_manifest(directory, filename, needed):
    directory = Path(directory).resolve()
    manifest = load(directory / filename)
    check(isinstance(manifest, dict) and needed <= set(manifest), 'Evidence manifest lacks required files')
    for name, digest in manifest.items():
        path = Path(name)
        check(not path.is_absolute() and '..' not in path.parts, 'Unsafe manifest path')
        target = (directory / path).resolve()
        check(target.is_relative_to(directory) and target.is_file(), 'Missing evidence file: ' + name)
        check(sha(target) == digest, 'Evidence hash differs: ' + name)
    return manifest


def inputs(root, snapshot, workbook_dir=None):
    snapshot = Path(snapshot).resolve()
    digest = sha(snapshot)
    if workbook_dir:
        paths = [Path(workbook_dir).resolve() / 'workbook_receipt.json']
    else:
        paths = [p for p in (root / 'output' / 'holistic_workbook').glob('*/workbook_receipt.json')
                 if load(p).get('status') == 'WORKBOOK_CREATED_REVIEW_REQUIRED'
                 and load(p).get('source', {}).get('snapshot_sha256') == digest]
    check(len(paths) == 1, 'Expected one matching original workbook; supply --workbook-dir if necessary')
    receipt_path = paths[0]
    receipt = load(receipt_path)
    check(receipt.get('status') == 'WORKBOOK_CREATED_REVIEW_REQUIRED', 'Original workbook receipt is incomplete')
    check(receipt['source']['snapshot_sha256'] == digest, 'Workbook snapshot hash differs')
    workbook = receipt_path.parent / Path(receipt['workbook']).name
    check(workbook.is_file() and sha(workbook) == receipt['workbook_sha256'], 'Original workbook changed; close Excel without saving it')
    module_path = Path(__file__).resolve().parent / 'wrike_report_analysis.py'
    check(sha(module_path) == receipt['source']['analysis_script_sha256'], 'Analysis helper differs from the original workbook version')
    import wrike_report_analysis as analysis
    check(Path(analysis.__file__).resolve() == module_path, 'Unexpected analysis module location')
    support = receipt_path.parent / 'supporting_calculations'
    verify_manifest(support, 'output_hashes.json', {'analysis_summary.json', 'project_effort.csv', 'task_audit.csv'})
    details = load(support / 'analysis_summary.json')
    check(details['metadata']['snapshot_sha256'] == digest, 'Supporting snapshot hash differs')
    check(details['summary'] == receipt['summary'], 'Workbook/supporting summaries differ')
    case = Path(details['metadata']['case_path']).resolve()
    capture, inventory, review = analysis.validate_case(case, digest)
    projects = {}
    all_ids = set()
    for row in rows(support / 'project_effort.csv'):
        pid = row['project_id']
        check(pid and pid not in all_ids, 'Invalid or repeated project effort ID')
        all_ids.add(pid)
        if row['in_frozen_snapshot'] == '0':
            check(row['access_group'] == 'ACCESSIBLE', 'Unexpected inaccessible API-only project')
            projects[pid] = row['project_name']
    expected = {pid for pid, row in review.items() if row['in_frozen_snapshot'] == '0'
                and row['current_type'] == 'PROJECT' and row['akash_access'] == 'RETRIEVED'}
    check(set(projects) == expected, 'Supplement project IDs differ from validated capture evidence')
    check(len(projects) == EXPECTED_PROJECTS, f'Expected exactly {EXPECTED_PROJECTS} verified projects, found {len(projects)}')
    check(all(re.fullmatch(r'[A-Za-z0-9_-]+', pid) for pid in projects), 'Unexpected project ID syntax')
    provenance = {'snapshot_path': str(snapshot), 'snapshot_sha256': digest,
                  'snapshot_export_timestamps': details['metadata']['snapshot_timestamps'],
                  'snapshot_data_refresh_values': details['metadata']['data_refresh_values'],
                  'workbook_path': str(workbook), 'workbook_sha256': receipt['workbook_sha256'],
                  'workbook_receipt_path': str(receipt_path), 'workbook_receipt_sha256': sha(receipt_path),
                  'access_case_path': str(case), 'access_manifest_sha256': sha(case / 'hashes.json'),
                  'support_manifest_sha256': sha(support / 'output_hashes.json'),
                  'analysis_script_sha256': sha(Path(analysis.__file__)), 'project_ids': sorted(projects)}
    frozen_ids = set()
    for row in rows(support / 'task_audit.csv'):
        check(row['task_id'] and row['task_id'] not in frozen_ids, 'Repeated/blank frozen task ID')
        frozen_ids.add(row['task_id'])
    check(len(frozen_ids) == details['summary']['unique_task_count'], 'Frozen task audit count differs')
    provenance['frozen_task_audit_sha256'] = sha(support / 'task_audit.csv')
    provenance['project_names'] = projects
    return projects, provenance, frozen_ids


def proxies_from_source(path):
    raw = Path(path).read_bytes()
    values = []
    for node in ast.walk(ast.parse(raw)):
        targets = node.targets if isinstance(node, ast.Assign) else ([node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else [])
        if any(isinstance(target, ast.Name) and target.id == 'PROXIES' for target in targets):
            check(not isinstance(node, ast.AugAssign), 'Dynamic proxy assignment requires review')
            values.append(ast.literal_eval(node.value))
    check(len(values) == 1, 'Expected one literal PROXIES assignment')
    proxy = values[0]
    check(proxy is None or (isinstance(proxy, dict) and all(k in ('http', 'https') and isinstance(v, str)
          for k, v in proxy.items())), 'Unexpected PROXIES configuration')
    return proxy, {'path': str(Path(path).resolve()), 'sha256': hashlib.sha256(raw).hexdigest()}


class Client:
    def __init__(self, token, proxy, output, project_ids):
        import requests
        self.requests = requests
        self.session = requests.Session()
        self.token, self.proxy, self.output = token, proxy, output
        self.allowed = {'folders/' + pid for pid in project_ids} | {'folders/' + pid + '/tasks' for pid in project_ids}
        self.sequence, self.last = 0, 0.0

    def get(self, endpoint, params=None):
        check(endpoint in self.allowed, 'Endpoint is outside the five verified project scopes')
        for attempt in range(1, 4):
            time.sleep(max(0, self.last + 0.4 - time.monotonic()))
            self.last = time.monotonic()
            self.sequence += 1
            started = now()
            status, body, error, delay = None, None, '', 2 ** attempt
            try:
                response = self.session.get('https://www.wrike.com/api/v4/' + endpoint,
                    params=params, headers={'Authorization': 'Bearer ' + self.token},
                    proxies=self.proxy, timeout=(15, 60), allow_redirects=False)
                status = response.status_code
                try:
                    body = response.json()
                except ValueError:
                    error = 'NON_JSON_RESPONSE'
                retry = response.headers.get('Retry-After')
                if retry:
                    try:
                        delay = float(retry)
                    except ValueError:
                        try:
                            date = parsedate_to_datetime(retry)
                            if date.tzinfo is None:
                                date = date.replace(tzinfo=timezone.utc)
                            delay = max(0, (date - datetime.now(timezone.utc)).total_seconds())
                        except (ValueError, TypeError, OverflowError):
                            delay = 60
            except self.requests.RequestException:
                error = 'NETWORK_ERROR'
            record = {'role_label': 'akash_user_supplied_token', 'endpoint': endpoint, 'params': params,
                      'started_at': started, 'finished_at': now(), 'attempt': attempt,
                      'http_status': status, 'error': error, 'response': body if status == 200 else None,
                      'api_error': body.get('error') if isinstance(body, dict) and status != 200 else None}
            filename = f'request_{self.sequence:05d}.json'
            save(self.output / filename, record)
            check(status != 401, 'Token rejected (401); capture stopped')
            if status in (429, 500, 502, 503, 504) or status is None:
                check(attempt < 3 and 0 <= delay <= 60, 'Request retry limit reached; partial evidence saved')
                print(f'Retry {attempt}/3, HTTP {status}, waiting {delay:g}s', flush=True)
                time.sleep(delay)
                continue
            check(status == 200 and isinstance(body, dict) and isinstance(body.get('data'), list),
                  f'Unexpected response for {endpoint}: HTTP {status}; partial evidence saved')
            return body, filename, record['finished_at']


def effort_class(task):
    if 'effortAllocation' not in task:
        return 'MISSING_EFFORT_FIELD', None
    allocation = task['effortAllocation']
    if not isinstance(allocation, dict):
        return 'NULL_OR_INVALID_ALLOCATION', None
    if 'totalEffort' not in allocation:
        expected_none = allocation.get('mode') == 'None' and allocation.get('responsibleAllocation') == []
        return ('MODE_NONE_WITHOUT_TOTAL' if expected_none else 'MISSING_TOTAL'), None
    raw = allocation['totalEffort']
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        return 'INVALID_TOTAL', None
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        return 'INVALID_TOTAL', None
    if not value.is_finite() or value < 0 or (value and abs(value.adjusted()) > 30):
        return 'INVALID_TOTAL', None
    if allocation.get('mode') == 'None' and value != 0:
        return 'MODE_NONE_WITH_POSITIVE_TOTAL', None
    return ('EXPLICIT_ZERO' if value == 0 else 'EXPLICIT_POSITIVE'), value


def project_tasks(client, pid, position):
    body, filename, observed = client.get('folders/' + pid)
    check(body.get('kind') == 'folders' and len(body['data']) == 1, 'Unexpected project lookup shape')
    folder = body['data'][0]
    check(isinstance(folder, dict) and folder.get('id') == pid and isinstance(folder.get('project'), dict)
          and folder.get('scope') != 'RbFolder', 'Project is no longer a retrievable nondeleted project')
    found, tokens, pages = {}, set(), []
    params = {'descendants': 'true', 'subTasks': 'true', 'pageSize': 1000, 'fields': json.dumps(FIELDS)}
    for page in range(1, 10001):
        body, filename, observed = client.get('folders/' + pid + '/tasks', params)
        check(body.get('kind') == 'tasks' and len(body['data']) <= 1000, 'Unexpected task page shape')
        for task in body['data']:
            check(isinstance(task, dict) and isinstance(task.get('id'), str)
                  and re.fullmatch(r'[A-Za-z0-9_-]+', task['id']), 'Invalid task ID')
            tid = task['id']
            check(tid not in found, 'Repeated task across pages; scope may have changed during capture')
            for relation in ('parentIds', 'superParentIds', 'superTaskIds', 'subTaskIds'):
                if relation in task:
                    check(isinstance(task[relation], list) and all(isinstance(v, str) and v for v in task[relation]),
                          'Malformed task relation: ' + relation)
            found[tid] = {'task': task, 'response_file': filename, 'observed_at': observed}
        pages.append(filename)
        cursor = body.get('nextPageToken')
        if cursor is None:
            print(f'Project {position}/{EXPECTED_PROJECTS}: {len(found):,} tasks across {page} page(s)', flush=True)
            missing_refs = sorted({child for r in found.values() for child in r['task'].get('subTaskIds', [])} - set(found))
            return folder, found, pages, missing_refs
        check(isinstance(cursor, str) and cursor and cursor not in tokens and body['data'], 'Invalid or cyclic pagination cursor')
        tokens.add(cursor)
        params = {**params, 'nextPageToken': cursor}
    raise ValueError('Project pagination exceeded safety limit')


def summarize(projects, captures, source, started, finished, request_count, frozen_ids):
    tasks = {}
    links, project_rows, issues = [], [], []
    for pid in sorted(captures):
        folder, found, pages, missing_refs = captures[pid]
        scope_issues = []
        for tid, observation in found.items():
            task = observation['task']
            if task.get('scope') != 'WsTask':
                scope_issues.append({'project_id': pid, 'task_id': tid, 'issue': 'UNEXPECTED_OR_MISSING_TASK_SCOPE'})
            if 'subTaskIds' not in task:
                scope_issues.append({'project_id': pid, 'task_id': tid, 'issue': 'REQUESTED_SUBTASK_IDS_FIELD_MISSING'})
            classification, minutes = effort_class(task)
            if tid in tasks:
                prior = tasks[tid]
                check(prior['effort_class'] == classification and prior['effort_minutes'] == minutes
                      and prior['effort_allocation_json'] == json.dumps(task.get('effortAllocation'), sort_keys=True),
                      'Shared task effort changed or conflicts across project captures: ' + tid)
                prior['project_ids'].add(pid)
                prior['response_files'].add(observation['response_file'])
            else:
                tasks[tid] = {'task_id': tid, 'task_name': task.get('title', ''), 'effort_class': classification,
                    'effort_minutes': minutes, 'effort_hours': minutes / 60 if minutes is not None else None,
                    'effort_allocation_json': json.dumps(task.get('effortAllocation'), sort_keys=True),
                    'project_ids': {pid}, 'response_files': {observation['response_file']},
                    'observed_at': observation['observed_at'], 'updated_at': task.get('updatedDate', ''),
                    'scope': task.get('scope', ''), 'status': task.get('status', ''),
                    'in_frozen_snapshot': int(tid in frozen_ids)}
            links.append({'project_id': pid, 'task_id': tid, 'effort_minutes': minutes,
                          'effort_hours': minutes / 60 if minutes is not None else None,
                          'response_file': observation['response_file'], 'membership_basis': 'SCOPED_FOLDER_TASK_LIST'})
        values = [tasks[tid]['effort_minutes'] for tid in found]
        known = sum((v for v in values if v is not None), Decimal(0))
        unknown = sum(v is None for v in values)
        complete = not unknown and not missing_refs and not scope_issues
        issues.extend(scope_issues)
        project_rows.append({'project_id': pid, 'project_name': folder.get('title', projects[pid]),
            'task_count': len(found), 'effort_minutes': known if complete else None,
            'effort_hours': known / 60 if complete else None, 'known_subtotal_minutes': known,
            'known_subtotal_hours': known / 60, 'unknown_effort_task_count': unknown,
            'unreturned_subtask_reference_count': len(missing_refs),
            'task_scope_issue_count': len(scope_issues),
            'unreturned_subtask_ids': ';'.join(missing_refs),
            'effort_status': 'CALCULATED_CURRENT_API' if complete else 'REVIEW_REQUIRED',
            'mode_none_without_total_count': sum(tasks[t]['effort_class'] == 'MODE_NONE_WITHOUT_TOTAL' for t in found),
            'explicit_numeric_task_count': len(found) - unknown,
            'other_unknown_count': sum(tasks[t]['effort_minutes'] is None and tasks[t]['effort_class'] != 'MODE_NONE_WITHOUT_TOTAL' for t in found),
            'tasks_already_in_snapshot': sum(t in frozen_ids for t in found),
            'tasks_absent_from_snapshot': sum(t not in frozen_ids for t in found),
            'known_hours_already_in_snapshot': sum((tasks[t]['effort_minutes'] for t in found if t in frozen_ids and tasks[t]['effort_minutes'] is not None), Decimal(0)) / 60,
            'known_hours_absent_from_snapshot': sum((tasks[t]['effort_minutes'] for t in found if t not in frozen_ids and tasks[t]['effort_minutes'] is not None), Decimal(0)) / 60,
            'page_count': len(pages), 'response_files': ';'.join(pages),
            'status': 'RETRIEVED_CURRENT_API', 'snapshot_effort_status': 'NOT_IN_FROZEN_SNAPSHOT'})
        for tid in missing_refs:
            issues.append({'project_id': pid, 'task_id': tid, 'issue': 'SUBTASK_REFERENCE_NOT_RETURNED_IN_PROJECT_LIST'})
    known = sum((r['effort_minutes'] for r in tasks.values() if r['effort_minutes'] is not None), Decimal(0))
    unknown = sum(r['effort_minutes'] is None for r in tasks.values())
    complete = not unknown and not issues
    task_rows = []
    for tid, row in sorted(tasks.items()):
        task_rows.append({**row, 'project_ids': ';'.join(sorted(row['project_ids'])),
                          'response_files': ';'.join(sorted(row['response_files']))})
    summary = {'status': 'EXTRA_PROJECT_CAPTURE_COMPLETE_REVIEW_REQUIRED', 'source': source,
               'started_at': started, 'finished_at': finished, 'project_count': len(project_rows),
               'unique_task_count': len(tasks), 'union_effort_minutes': known if complete else None,
               'union_effort_hours': known / 60 if complete else None,
               'known_union_effort_minutes': known, 'known_union_effort_hours': known / 60,
               'unknown_effort_task_count': unknown,
               'unreturned_subtask_reference_count': sum(r['issue'] == 'SUBTASK_REFERENCE_NOT_RETURNED_IN_PROJECT_LIST' for r in issues),
               'task_scope_issue_count': sum(r['issue'] != 'SUBTASK_REFERENCE_NOT_RETURNED_IN_PROJECT_LIST' for r in issues),
               'effort_class_counts': dict(Counter(r['effort_class'] for r in tasks.values())),
               'mode_none_without_total_count': sum(r['effort_class'] == 'MODE_NONE_WITHOUT_TOTAL' for r in tasks.values()),
               'explicit_numeric_task_count': len(tasks) - unknown,
               'other_unknown_count': sum(r['effort_minutes'] is None and r['effort_class'] != 'MODE_NONE_WITHOUT_TOTAL' for r in tasks.values()),
               'tasks_already_in_snapshot': sum(t in frozen_ids for t in tasks),
               'tasks_absent_from_snapshot': sum(t not in frozen_ids for t in tasks),
               'request_count': request_count, 'project_ids': sorted(projects),
               'independent_full_extraction_validation': False,
               'notes': ['Current live supplement captured after the frozen Snowflake snapshot; do not add to its reconciled total.',
                         'Task IDs deduplicated once within each project and across these five projects.',
                         'The same task can also exist under another project in the frozen snapshot; this capture does not prove all tasks are new.',
                         'Folder task requests include descendants and subtasks, all statuses, with no time/status filters.',
                         'Absence of an explicit totalEffort remains unknown; it is not silently converted to zero.',
                         'Token role is supplied by the user; account ownership is not independently verified.',
                         'Successful scoped API pagination proves only returned records visible to this token during this capture.']}
    return summary, project_rows, task_rows, links, issues


def validate_saved_capture(directory):
    """Validate all hashes, source evidence, raw pagination and derived rows offline."""
    directory = Path(directory).resolve()
    check(not (directory / 'capture_incomplete.json').exists(), 'Supplement capture is incomplete')
    manifest = verify_manifest(directory, 'hashes.json', {'inputs.json', 'summary.json', 'project_effort.csv',
                              'task_effort.csv', 'project_task_links.csv', 'review_issues.csv'})
    actual_files = {str(p.relative_to(directory)) for p in directory.rglob('*') if p.is_file() and p.name != 'hashes.json'}
    check(actual_files == set(manifest), 'Supplement manifest does not cover every evidence file')
    summary = load(directory / 'summary.json')
    check(summary.get('status') == 'EXTRA_PROJECT_CAPTURE_COMPLETE_REVIEW_REQUIRED', 'Supplement status is incomplete')
    source = load(directory / 'inputs.json')
    check(summary['source'] == source, 'Supplement source evidence differs')
    snapshot = Path(source['snapshot_path']).resolve()
    receipt = Path(source['workbook_receipt_path']).resolve()
    projects, verified, frozen_ids = inputs(snapshot.parent, snapshot, receipt.parent)
    check(all(source.get(k) == value for k, value in verified.items()), 'Supplement source provenance differs from verified evidence')
    check(source['capture_script_sha256'] == sha(Path(__file__)), 'Supplement helper differs from the capture version')
    start = datetime.fromisoformat(summary['started_at'])
    finish = datetime.fromisoformat(summary['finished_at'])
    check(start.tzinfo is not None and finish.tzinfo is not None and start <= finish, 'Invalid supplement capture times')
    evidence = []
    for file in sorted((directory / 'raw').glob('*.json')):
        item = load(file)
        first, last = datetime.fromisoformat(item['started_at']), datetime.fromisoformat(item['finished_at'])
        check(first.tzinfo is not None and last.tzinfo is not None and start <= first <= last <= finish,
              'Raw supplement request is outside recorded capture window')
        check(item['role_label'] == 'akash_user_supplied_token', 'Unexpected supplement token role')
        allowed = {'folders/' + pid for pid in projects} | {'folders/' + pid + '/tasks' for pid in projects}
        check(item['endpoint'] in allowed, 'Raw request is outside supplement project scope')
        check(item['http_status'] == 200 or item['http_status'] in (429, 500, 502, 503, 504, None),
              'Unresolved HTTP error in supposedly complete capture')
        if item['http_status'] != 200:
            check(item['response'] is None, 'Error response body should not be recorded')
        evidence.append((file.name, item))
    check(len(evidence) == summary['request_count'], 'Supplement request count differs')
    successes = [(name, item) for name, item in evidence if item['http_status'] == 200]
    captures = {}
    consumed = set()
    for pid in sorted(projects):
        lookup = [(name, item) for name, item in successes if item['endpoint'] == 'folders/' + pid]
        check(len(lookup) == 1, 'Expected exactly one successful project lookup')
        name, item = lookup[0]
        consumed.add(name)
        body = item['response']
        check(item['params'] is None and body.get('kind') == 'folders' and len(body['data']) == 1,
              'Unexpected saved project lookup')
        folder = body['data'][0]
        check(folder.get('id') == pid and isinstance(folder.get('project'), dict) and folder.get('scope') != 'RbFolder',
              'Saved project lookup does not establish a live project')
        listed = [(name, item) for name, item in successes if item['endpoint'] == 'folders/' + pid + '/tasks']
        check(listed, 'Missing saved task listing')
        expected = {'descendants': 'true', 'subTasks': 'true', 'pageSize': 1000, 'fields': json.dumps(FIELDS)}
        tasks, pages, tokens = {}, [], set()
        for number, (name, item) in enumerate(listed):
            check(item['params'] == expected, 'Saved task request parameters or pagination chain differ')
            body = item['response']
            check(body.get('kind') == 'tasks' and isinstance(body.get('data'), list) and len(body['data']) <= 1000,
                  'Saved task page shape differs')
            for task in body['data']:
                tid = task.get('id')
                check(isinstance(tid, str) and re.fullmatch(r'[A-Za-z0-9_-]+', tid) and tid not in tasks,
                      'Invalid/duplicate saved task ID')
                for relation in ('parentIds', 'superParentIds', 'superTaskIds', 'subTaskIds'):
                    if relation in task:
                        check(isinstance(task[relation], list) and all(isinstance(v, str) and v for v in task[relation]),
                              'Malformed saved task relation')
                tasks[tid] = {'task': task, 'response_file': name, 'observed_at': item['finished_at']}
            consumed.add(name)
            pages.append(name)
            cursor = body.get('nextPageToken')
            if cursor is None:
                check(number == len(listed) - 1, 'Extra successful page after pagination termination')
            else:
                check(isinstance(cursor, str) and cursor and cursor not in tokens and body['data']
                      and number < len(listed) - 1, 'Unfinished or cyclic saved pagination')
                tokens.add(cursor)
                expected = {**expected, 'nextPageToken': cursor}
        missing = sorted({child for r in tasks.values() for child in r['task'].get('subTaskIds', [])} - set(tasks))
        captures[pid] = folder, tasks, pages, missing
    check(consumed == {name for name, _ in successes}, 'Unconsumed successful API evidence')
    rebuilt = summarize(projects, captures, source, summary['started_at'], summary['finished_at'], len(evidence), frozen_ids)
    expected_summary, project_rows, task_rows, links, issues = rebuilt
    check(json.loads(json.dumps(expected_summary, default=str)) == summary, 'Saved supplement summary differs from raw evidence')
    def serialized(records):
        return [{key: '' if value is None else str(value) for key, value in row.items()} for row in records]
    for filename, records in [('project_effort.csv', project_rows), ('task_effort.csv', task_rows),
                              ('project_task_links.csv', links), ('review_issues.csv', issues)]:
        check(list(rows(directory / filename)) == serialized(records), 'Saved supplement CSV differs from raw evidence: ' + filename)
    return rebuilt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', default='snowflake_snapshot.csv')
    parser.add_argument('--workbook-dir')
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    root = Path.cwd()
    print('Checking frozen snapshot, workbook and saved access evidence...', flush=True)
    projects, source, frozen_ids = inputs(root, args.snapshot, args.workbook_dir)
    proxy, proxy_source = proxies_from_source(root / 'Wrike_Data_local_validation.py')
    source['proxy_source'] = proxy_source
    source['capture_script_sha256'] = sha(Path(__file__))
    print('Verified API-only projects:', len(projects), flush=True)
    for pid, name in sorted(projects.items()):
        print(pid, '|', name, flush=True)
    if args.check_only:
        print('OFFLINE CHECK PASSED. No API requests made.')
        return
    import requests  # Check installed dependency before token entry.
    output = root / 'output' / 'extra_project_effort' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    output.mkdir(parents=True, exist_ok=False)
    raw = output / 'raw'
    raw.mkdir()
    save(output / 'inputs.json', source)
    started = now()
    captures = {}
    try:
        token = getpass.getpass("Paste AKASH's token (hidden): ").strip()
        check(bool(token), 'No token entered')
        client = Client(token, proxy, raw, projects)
        for position, pid in enumerate(sorted(projects), 1):
            captures[pid] = project_tasks(client, pid, position)
        finished = now()
        summary, project_rows, task_rows, links, issues = summarize(projects, captures, source, started, finished, client.sequence, frozen_ids)
        save(output / 'summary.json', summary)
        csv_write(output / 'project_effort.csv', project_rows, list(project_rows[0]))
        task_fields = ['task_id', 'task_name', 'effort_class', 'effort_minutes', 'effort_hours', 'effort_allocation_json',
                       'project_ids', 'response_files', 'observed_at', 'updated_at', 'scope', 'status', 'in_frozen_snapshot']
        csv_write(output / 'task_effort.csv', task_rows, task_fields)
        csv_write(output / 'project_task_links.csv', links, ['project_id', 'task_id', 'effort_minutes', 'effort_hours', 'response_file', 'membership_basis'])
        csv_write(output / 'review_issues.csv', issues, ['project_id', 'task_id', 'issue'])
        print('CURRENT API SUPPLEMENT CAPTURED — review required', flush=True)
        print('Projects:', summary['project_count'], '| Unique tasks:', summary['unique_task_count'])
        for row in project_rows:
            amount = f"{row['effort_hours']:,.6f}" if row['effort_hours'] is not None else 'UNKNOWN'
            print(row['project_id'], '|', row['project_name'], '| Tasks:', row['task_count'], '| Hours:', amount,
                  '| Recorded subtotal:', f"{row['known_subtotal_hours']:,.6f}",
                  '| Mode None:', row['mode_none_without_total_count'], '| Other unknown:', row['other_unknown_count'],
                  '| Scope issues:', row['unreturned_subtask_reference_count'] + row['task_scope_issue_count'])
        print('Known union subtotal hours:', f"{summary['known_union_effort_hours']:,.6f}")
        print('No total, mode None:', summary['mode_none_without_total_count'], '| Other unknown effort:', summary['other_unknown_count'])
        print('Tasks already in frozen snapshot:', summary['tasks_already_in_snapshot'], '| Tasks absent:', summary['tasks_absent_from_snapshot'])
        print('Unreturned subtask references:', summary['unreturned_subtask_reference_count'], '| Other scope issues:', summary['task_scope_issue_count'])
        print('Separate capture window:', started, 'to', finished)
        print('Do not add this later live supplement to the frozen snapshot total.')
    except BaseException as error:
        save(output / 'capture_incomplete.json', {'status': 'INCOMPLETE', 'started_at': started, 'finished_at': now(),
             'completed_project_ids': sorted(captures), 'exception_class': type(error).__name__})
        raise
    finally:
        save(output / 'hashes.json', {str(p.relative_to(output)): sha(p) for p in output.rglob('*')
                                     if p.is_file() and p.name != 'hashes.json'})
        print('OUTPUT:', output, flush=True)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, csv.Error) as error:
        raise SystemExit('CAPTURE STOPPED: ' + str(error))
