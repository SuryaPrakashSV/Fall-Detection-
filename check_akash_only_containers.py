#!/usr/bin/env python3
"""Check four Akash-only containers with YOUR token, plus one shared control.

Save in wrike-local-baseline beside review_wrike_effort_gaps.py and
check_missing_wrike_ids.py. This uses their existing offline validation and
bounded HTTP lookup logic. No full extraction, source changes or external writes.
Token input is hidden and never saved. Current results are not historical proof.
"""
import argparse
import csv
import getpass
import json
import sys
import warnings
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests
import review_wrike_effort_gaps as gap
import check_missing_wrike_ids as shared

ACCESS_COMPARISON = 'output/akash_comparison/20260925T002311_148327Z_73a909'


def prepare(root, report=None):
    if report is None:
        reports = sorted((root / 'output' / 'effort_reconciliation').glob('*/effort_reconciliation_details.json'))
        if not reports:
            raise ValueError('No saved effort reconciliation report found.')
        report = reports[-1].parent
    path = report / 'effort_reconciliation_details.json'
    before = gap.sha(path)
    details = json.loads(path.read_text(encoding='utf-8-sig'))
    comparison = details['comparisons'][gap.OLD]
    filename = comparison['task_difference_file']
    if Path(filename).name != filename:
        raise ValueError('Invalid difference filename')
    rows, audit = gap.read_differences(report / filename, comparison)
    rows = [row for row in rows if row['difference_type'] == 'AKASH_ONLY']
    if len(rows) != 635:
        raise ValueError('Expected the verified 635 Akash-only tasks.')
    gap.same(gap.total(rows, '_b'), '43432', 'Verified Akash-only effort hours')
    targets = {}
    for row in rows:
        ids = row['_akash_direct_container_ids']
        if len(ids) != 1:
            raise ValueError('Expected one direct container for each of these 635 tasks.')
        entity_id = ids[0]
        shared.check_id(entity_id)
        item = targets.setdefault(entity_id, {'task_count': 0, 'effort_hours': Decimal(0)})
        item['task_count'] += 1
        item['effort_hours'] += row['_b']
    if len(targets) != 4:
        raise ValueError('Expected exactly four target containers.')
    names, name_note = gap.source_names(root, details['datasets']['akash']['input'], set(targets))
    for entity_id, item in targets.items():
        item['saved_names'] = names.get(entity_id, [])
        item['effort_hours'] = str(item['effort_hours'])
    # This only reads saved comparison files and literal proxy configuration.
    # It does not invoke the older 61-ID live check or extraction script.
    context = shared.prepare(root, root / ACCESS_COMPARISON)
    if context['control'] in targets:
        raise ValueError('Control unexpectedly overlaps the four targets.')
    if gap.sha(path) != before:
        raise ValueError('Report changed while preparing checks.')
    return {'report': str(report), 'details_sha256': before, 'difference_input': audit,
            'name_note': name_note, 'targets': targets, 'control': context['control'],
            'proxies': context['proxies'], 'proxy_source': context['source_audit']}


def execute(root, context, token, session=None):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    out = root / 'output' / 'effort_container_access' / stamp
    out.mkdir(parents=True, exist_ok=False)
    report = {k: v for k, v in context.items() if k != 'proxies'}
    report.update(started_at_utc=shared.utc_now(),
        token_label='Surya token supplied by operator; identity not independently verified',
        scope='One shared control followed by exactly four Akash-only containers',
        status='RUNNING', attempts=[], control_result=None,
        results={entity_id: {'outcome': 'NOT_CHECKED'} for entity_id in context['targets']},
        limitation='Current direct folder lookups do not prove September 24 permissions, historical causes, or task-level completeness.')

    def save():
        temp = out / 'access_results.json.partial'
        temp.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        temp.replace(out / 'access_results.json')

    def emit(role, entity_id, attempt, status, reason, delay):
        report['attempts'].append({'checked_at_utc': shared.utc_now(), 'role': role,
            'container_id': entity_id, 'attempt': attempt, 'http_status': status,
            'reason': reason, 'retry_wait_seconds': delay})
        save()

    save()
    owned = session is None
    session = requests.Session() if owned else session
    try:
        probe = shared.Probe(session, token, context['proxies'], emit)
        result, stop = probe.lookup(context['control'], 'shared_control')
        report['control_result'] = result
        print('Shared control:', result['outcome'], result['http_status'], result['reason'], flush=True)
        if stop or result['outcome'] != 'RETRIEVED':
            report['status'] = 'STOPPED_CONTROL_NOT_RETRIEVED'
            return out
        for entity_id, item in context['targets'].items():
            result, stop = probe.lookup(entity_id, 'akash_only_container')
            report['results'][entity_id] = result
            title = ' / '.join(item['saved_names']) or entity_id
            print(f'{title}: {result["outcome"]}; HTTP {result["http_status"]}; '
                  f'{item["task_count"]} saved tasks; {item["effort_hours"]} saved hours', flush=True)
            if result['api_title'] and result['api_title'] not in item['saved_names']:
                print('  Current API title:', result['api_title'], flush=True)
            save()
            if stop:
                report['status'] = 'STOPPED_REQUEST_ERROR'
                return out
        report['status'] = 'CHECKS_FINISHED'
        return out
    except KeyboardInterrupt:
        report['status'] = 'INTERRUPTED'
        raise
    except Exception:
        report['status'] = 'STOPPED_UNEXPECTED_ERROR'
        raise
    finally:
        if owned:
            session.close()
        report['finished_at_utc'] = shared.utc_now()
        save()
        print('Target outcomes:', dict(Counter(r['outcome'] for r in report['results'].values())))
        print('Saved:', out / 'access_results.json')
        print(report['limitation'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, help='Explicit saved effort reconciliation folder')
    parser.add_argument('--check-inputs', action='store_true', help='Offline validation only; no token or API calls')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    context = prepare(root, args.report.expanduser().resolve() if args.report else None)
    print('Verified four containers: 635 saved tasks; 43,432 planned effort hours.')
    for entity_id, item in context['targets'].items():
        print(entity_id, ' / '.join(item['saved_names']), item['task_count'], item['effort_hours'])
    if args.check_inputs:
        print('Offline input check complete. No API calls made.')
        return
    print('Enter YOUR Wrike token, not Akash\'s. Five container IDs only; no full extraction.')
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        token = getpass.getpass('Your Wrike token (hidden): ').strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError('Empty token or whitespace in token; no calls made.')
    execute(root, context, token)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, csv.Error, getpass.GetPassWarning) as exc:
        print('STOPPED: ' + str(exc), file=sys.stderr)
        sys.exit(1)
