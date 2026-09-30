#!/usr/bin/env python3
"""Read-only project access evidence for a frozen Snowflake snapshot.

Run on the Mac in wrike-local-baseline. Only GET /folders and GET /folders/{id}.
No task extraction, no Snowflake connection, no source execution or modification.
Tokens are requested privately and never saved. PROXIES is read with ast.literal_eval.
This captures current project access, not historical/task-level access or effort.
Official parameters: https://developers.wrike.com/reference/getfoldersempty
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
from email.utils import parsedate_to_datetime
from pathlib import Path

FOLDERS = ['parent_folder_id', 'child_folder_id', 'grandchild_folder_id',
           'baby_folder_id', 'grandbaby_folder_id']
TASKS = ['parent_task_id', 'child_task_id', 'grandchild_task_id',
         'baby_task_id', 'grandbaby_task_id', 'great_grandbaby_task_id']
NULLS = {'', 'nan', 'none', 'null', 'placeholder'}


def now():
    return datetime.now(timezone.utc).isoformat()


def clean(v):
    v = (v or '').strip()
    return '' if v.lower() in NULLS else v


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def save(path, value):
    with path.open('x', encoding='utf-8') as f:
        json.dump(value, f, indent=2, ensure_ascii=False)


def scan(path, expected):
    before = (path.stat().st_size, path.stat().st_mtime_ns)
    csv.field_size_limit(sys.maxsize)
    entities, stamps, refreshes = {}, Counter(), Counter()
    count = 0
    with path.open(encoding='utf-8-sig', newline='') as f:
        r = csv.DictReader(f)
        original = r.fieldnames or []
        normalized = [x.strip().lower() for x in original]
        if len(set(normalized)) != len(normalized) or '' in normalized:
            raise ValueError('Blank or duplicate CSV headers')
        required = {'id', 'key', 'title', 'effortallocation_totaleffort',
                    'snapshot_exported_at', *FOLDERS, *TASKS}
        if required - set(normalized):
            raise ValueError('Missing columns: ' + ', '.join(sorted(required-set(normalized))))
        r.fieldnames = normalized
        for row in r:
            count += 1
            if None in row or any(v is None for v in row.values()):
                raise ValueError('Malformed CSV row ' + str(count))
            stamps[row['snapshot_exported_at'].strip()] += 1
            refreshes[row.get('data refresh', '').strip()] += 1
            container, key = clean(row['id']), clean(row['key'])
            ids = {clean(row[x]) for x in FOLDERS} | {container}
            for fid in ids - {''}:
                if not re.fullmatch(r'[A-Za-z0-9_-]+', fid):
                    raise ValueError('Unexpected container ID format at row ' + str(count))
                item = entities.setdefault(fid, {'id': fid, 'names': set(),
                    'own_project_flags': set(), 'own_rows': 0, 'scope_rows': 0})
                item['scope_rows'] += 1
                if key == fid:
                    item['own_rows'] += 1
                    if clean(row['title']):
                        item['names'].add(row['title'].strip())
                    if clean(row.get('is_project')):
                        item['own_project_flags'].add(row['is_project'].strip())
                for col in FOLDERS:
                    if clean(row[col]) == fid:
                        title = clean(row.get(col[:-3] + '_title'))
                        if title:
                            item['names'].add(title)
            if count % 50000 == 0:
                print('Snapshot rows read:', count, flush=True)
    digest = sha(path)
    if before != (path.stat().st_size, path.stat().st_mtime_ns):
        raise ValueError('Snapshot changed while reading')
    if count != expected:
        raise ValueError(f'Expected {expected} rows; found {count}')
    if len(stamps) != 1 or not clean(next(iter(stamps), '')):
        raise ValueError('Expected one nonblank snapshot export timestamp')
    for item in entities.values():
        item['names'] = sorted(item['names'])
        item['own_project_flags'] = sorted(item['own_project_flags'])
    return entities, {'path': str(path.resolve()), 'sha256': digest,
        'bytes': before[0], 'data_rows': count, 'columns': original,
        'snapshot_timestamps': dict(stamps), 'data_refresh_values': dict(refreshes),
        'expected_rows_basis': 'CLI expected count; verify against Snowflake query result'}


def proxies_from_source(path):
    raw = path.read_bytes()
    tree = ast.parse(raw)
    nodes = []
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else [])
        if any(isinstance(x, ast.Name) and x.id == 'PROXIES' for x in targets):
            if isinstance(node, ast.AugAssign):
                raise ValueError('Dynamic PROXIES; stop for review')
            nodes.append(ast.literal_eval(node.value))
    if len(nodes) != 1:
        raise ValueError('Expected one literal PROXIES assignment; stop for review')
    p = nodes[0]
    if p is not None and (not isinstance(p, dict) or any(
            k not in ('http', 'https') or not isinstance(v, str) for k, v in p.items())):
        raise ValueError('Unexpected PROXIES configuration')
    return p, {'path': str(path.resolve()), 'sha256': hashlib.sha256(raw).hexdigest()}


class Client:
    def __init__(self, token, proxies, output, role):
        import requests
        self.requests = requests
        self.session = requests.Session()
        self.token, self.proxies, self.output, self.role = token, proxies, output, role
        self.sequence, self.last = 0, 0.0

    def get(self, endpoint, params=None):
        if endpoint != 'folders' and not re.fullmatch(r'folders/[A-Za-z0-9_-]+', endpoint):
            raise ValueError('Unexpected endpoint')
        for attempt in range(1, 4):
            time.sleep(max(0, self.last + 0.4 - time.monotonic()))
            self.last = time.monotonic()
            started = now()
            self.sequence += 1
            status, body, error, delay = None, None, '', 2 ** attempt
            try:
                r = self.session.get('https://www.wrike.com/api/v4/' + endpoint,
                    params=params, headers={'Authorization': 'Bearer ' + self.token},
                    proxies=self.proxies, timeout=(15, 60), allow_redirects=False)
                status = r.status_code
                try:
                    body = r.json()
                except ValueError:
                    error = 'NON_JSON_RESPONSE'
                retry_header = r.headers.get('Retry-After')
                if retry_header:
                    try:
                        delay = float(retry_header)
                    except ValueError:
                        try:
                            when = parsedate_to_datetime(retry_header)
                            if when.tzinfo is None:
                                when = when.replace(tzinfo=timezone.utc)
                            delay = max(0, (when-datetime.now(timezone.utc)).total_seconds())
                        except (ValueError, TypeError, OverflowError):
                            delay = 60
            except self.requests.RequestException:
                error = 'NETWORK_ERROR'
            # Never save headers, request objects, exception text or raw error bodies.
            evidence = {'role_label': self.role, 'endpoint': endpoint, 'params': params,
                'started_at': started, 'finished_at': now(), 'attempt': attempt,
                'http_status': status, 'error': error,
                'response': body if status == 200 else None,
                'api_error': body.get('error') if isinstance(body, dict) and status != 200 else None}
            save(self.output / f'{self.role}_{self.sequence:05d}.json', evidence)
            if status == 401:
                raise ValueError('Token rejected with HTTP 401; capture stopped')
            retry = status in (429, 500, 502, 503, 504) or status is None
            if retry:
                if attempt == 3 or not 0 <= delay <= 60:
                    raise ValueError('Request retries exhausted or excessive retry delay; evidence saved')
                print(f'{self.role}: retry {attempt}/3, HTTP {status}, waiting {delay}s', flush=True)
                time.sleep(delay)
                continue
            return status, body, evidence['finished_at']

    def inventory(self, is_project):
        found, seen = {}, set()
        params = {'project': 'true' if is_project else 'false', 'deleted': 'false', 'pageSize': 1000}
        for page in range(1, 1001):
            status, body, stamp = self.get('folders', params)
            if status != 200 or not isinstance(body, dict) or body.get('kind') != 'folders' or not isinstance(body.get('data'), list):
                raise ValueError('Inventory failed or unexpected response; stop')
            for item in body['data']:
                if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                    raise ValueError('Malformed inventory record')
                if isinstance(item.get('project'), dict) != is_project:
                    raise ValueError('Inventory project filter/type mismatch')
                fid = item['id']
                if fid in found:
                    raise ValueError('Duplicate ID across inventory pages; review capture drift')
                found[fid] = {'item': item, 'observed_at': stamp, 'via': 'inventory'}
            print(f'{self.role}: {"projects" if is_project else "folders"} page {page}, total {len(found)}', flush=True)
            cursor = body.get('nextPageToken')
            if not cursor:
                return found
            if not isinstance(cursor, str) or cursor in seen:
                raise ValueError('Invalid or repeated pagination cursor')
            seen.add(cursor)
            params = {**params, 'nextPageToken': cursor}
        raise ValueError('Pagination exceeded safety bound')

    def lookup(self, fid):
        status, body, stamp = self.get('folders/' + fid)
        if status == 200 and isinstance(body, dict) and body.get('kind') == 'folders':
            items = body.get('data')
            if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict) and items[0].get('id') == fid:
                return {'outcome': 'RETRIEVED', 'http_status': status,
                    'item': items[0], 'observed_at': stamp, 'via': 'direct_lookup'}
        if status in (403, 404) and isinstance(body, dict) and isinstance(body.get('error'), str):
            return {'outcome': 'FORBIDDEN' if status == 403 else 'NOT_FOUND',
                'http_status': status, 'observed_at': stamp, 'via': 'direct_lookup'}
        return {'outcome': 'UNRESOLVED', 'http_status': status,
            'observed_at': stamp, 'via': 'direct_lookup'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--snapshot', default='snowflake_snapshot.csv')
    p.add_argument('--expected-rows', type=int, required=True)
    p.add_argument('--check-only', action='store_true')
    args = p.parse_args()
    root = Path.cwd()
    entities, audit = scan(Path(args.snapshot), args.expected_rows)
    proxies, source = proxies_from_source(root / 'Wrike_Data_local_validation.py')
    print('Snapshot rows:', audit['data_rows'])
    print('Snapshot timestamp:', audit['snapshot_timestamps'])
    print('Container IDs to reconcile:', len(entities))
    if args.check_only:
        print('OFFLINE CHECK PASSED. No API calls made.')
        return
    # Import before prompting so a missing dependency never wastes a token entry.
    import requests
    output = root / 'output' / 'project_access_today' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    output.mkdir(parents=True, exist_ok=False)
    raw = output / 'raw'
    raw.mkdir()
    save(output / 'snapshot_inventory.json', {'audit': audit, 'entities': entities, 'source': source})
    print('Output:', output, flush=True)
    started = now()
    try:
        token = getpass.getpass("Paste AKASH's token (hidden): ").strip()
        if not token:
            raise ValueError('No token entered')
        client = Client(token, proxies, raw, 'akash')
        projects = client.inventory(True)
        folders = client.inventory(False)
        if set(projects) & set(folders):
            raise ValueError('Project/folder inventory overlap; capture needs review')
        visible = {**projects, **folders}
        absent = sorted(set(entities)-set(visible))
        if len(absent) > 500:
            raise ValueError(f'{len(absent)} direct checks required; stop to review scope/token')
        lookups = {}
        for i, fid in enumerate(absent, 1):
            lookups[fid] = client.lookup(fid)
            if i % 10 == 0 or i == len(absent):
                print(f'Akash direct checks: {i}/{len(absent)}', flush=True)
        save(output / 'akash_access.json', {'projects': projects, 'folders': folders, 'direct_checks': lookups})
        # Surya metadata identifies project/folder type for IDs unavailable to Akash.
        # It does not determine Akash access or replace his observed result.
        type_needed = [fid for fid in absent if lookups[fid]['outcome'] != 'RETRIEVED']
        other = {}
        if type_needed:
            print(f'{len(type_needed)} IDs need independent current type evidence.', flush=True)
            extra = getpass.getpass("Paste YOUR (Surya) token to check their types, or Enter to leave unverified: ").strip()
            if extra:
                other_client = Client(extra, proxies, raw, 'surya_type_only')
                for i, fid in enumerate(type_needed, 1):
                    other[fid] = other_client.lookup(fid)
                    if i % 10 == 0 or i == len(type_needed):
                        print(f'Type checks: {i}/{len(type_needed)}', flush=True)
        save(output / 'supplemental_type_evidence.json', other)
        rows = []
        for fid in sorted(set(entities) | set(projects)):
            sf = entities.get(fid, {})
            a = visible.get(fid) or lookups.get(fid, {})
            item = a.get('item') or other.get(fid, {}).get('item') or {}
            kind = ('PROJECT' if isinstance(item.get('project'), dict) else 'FOLDER') if item else 'UNVERIFIED'
            aitem = a.get('item', {})
            scope = aitem.get('scope', '')
            access = ('RECYCLE_BIN' if scope == 'RbFolder' else 'RETRIEVED') if aitem else a.get('outcome', 'UNRESOLVED')
            rows.append({'container_id': fid, 'name': item.get('title') or ' | '.join(sf.get('names', [])),
                'current_type': kind, 'type_evidence': 'akash' if aitem else ('surya' if item else 'none'),
                'in_frozen_snapshot': int(fid in entities), 'akash_access': access,
                'akash_http_status': a.get('http_status', 200 if aitem else ''),
                'akash_observed_at': a.get('observed_at', ''), 'akash_scope': scope,
                'akash_evidence_method': a.get('via', ''),
                'snapshot_own_project_flags': ' | '.join(sf.get('own_project_flags', [])),
                'note': '403/404 do not prove the historical cause; no task-level access claim'})
        with (output / 'project_access_review.csv').open('x', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ['container_id'])
            w.writeheader()
            w.writerows(rows)
        summary = {'status': 'ACCESS_CAPTURE_COMPLETE_REVIEW_REQUIRED',
            'started_at': started, 'finished_at': now(), 'snapshot': audit,
            'token_identity': 'User-entered role labels; owner identity not independently verified',
            'visible_project_inventory_count': len(projects), 'visible_folder_inventory_count': len(folders),
            'baseline_container_count': len(entities), 'direct_check_count': len(lookups),
            'classification_counts': dict(Counter(r['current_type']+' / '+r['akash_access'] for r in rows)),
            'notes': ['Current access checks occurred after the frozen snapshot.',
                'No effort calculated here. Planned effort must be derived from the frozen CSV.',
                'Project retrieval does not establish access to every task in its snapshot scope.',
                'API-only projects have unknown snapshot effort, not zero.',
                'This is not full extraction validation or historical deletion proof.']}
        save(output / 'summary.json', summary)
        files = [f for f in output.rglob('*') if f.is_file()]
        save(output / 'hashes.json', {str(f.relative_to(output)): sha(f) for f in sorted(files)})
        print('\nACCESS CAPTURE COMPLETE — review required')
        print('Visible project inventory:', len(projects))
        for label, n in summary['classification_counts'].items():
            print(label + ':', n)
        print('OUTPUT:', output)
        print('Share these final counts and the output path. Do not share tokens.')
    except (Exception, KeyboardInterrupt) as error:
        # Controlled diagnostics only: credentials must never appear in tracebacks.
        msg = str(error) if isinstance(error, ValueError) else type(error).__name__
        save(output / 'STOPPED.json', {'status': 'INCOMPLETE', 'time': now(), 'reason': msg})
        print('STOPPED:', msg)
        print('Partial evidence saved:', output)
        raise SystemExit(2)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as exc:
        print('PREPARATION STOPPED:', str(exc))
        raise SystemExit(2)
