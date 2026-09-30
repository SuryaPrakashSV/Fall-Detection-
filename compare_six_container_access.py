#!/usr/bin/env python3
"""Compare current retrieval of six effort-gap containers with two tokens.
Run beside review_wrike_effort_gaps.py and check_missing_wrike_ids.py.
Reads saved evidence; makes only direct folder GETs. Never saves tokens.
"""
import argparse
import csv
import getpass
import hashlib
import json
import sys
import warnings
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests
import review_wrike_effort_gaps as gap
import check_missing_wrike_ids as shared

ACCESS_COMPARISON = 'output/akash_comparison/20260925T002311_148327Z_73a909'


def prepare(root, report=None):
    if report is None:
        matches = sorted((root / 'output/effort_reconciliation').glob('*/effort_reconciliation_details.json'))
        if not matches:
            raise ValueError('No effort reconciliation report found.')
        report = matches[-1].parent
    path = report / 'effort_reconciliation_details.json'
    digest = gap.sha(path)
    details = json.loads(path.read_text(encoding='utf-8-sig'))
    comparison = details['comparisons'][gap.OLD]
    filename = comparison['task_difference_file']
    if Path(filename).name != filename:
        raise ValueError('Unexpected difference file path.')
    rows, audit = gap.read_differences(report / filename, comparison)
    targets = {}
    notes = {}
    for kind, side, source, count, hours, expected_groups in (
        ('BASELINE_ONLY_OUTSIDE_56', 'baseline', 'surya', 172, '22845.5', 2),
        ('AKASH_ONLY', 'akash', 'akash', 635, '43432', 4),
    ):
        subset = [r for r in rows if r['difference_type'] == kind]
        metric = '_a' if side == 'baseline' else '_b'
        if len(subset) != count:
            raise ValueError('Unexpected saved task count: ' + kind)
        gap.same(gap.total(subset, metric), hours, kind)
        groups = {}
        for row in subset:
            ids = row['_' + side + '_direct_container_ids']
            if len(ids) != 1:
                raise ValueError('Expected one direct container per selected task.')
            entity_id = ids[0]
            shared.check_id(entity_id)
            item = groups.setdefault(entity_id, dict(saved_difference_type=kind,
                saved_task_count=0, saved_effort_hours=Decimal(0)))
            item['saved_task_count'] += 1
            item['saved_effort_hours'] += row[metric]
        if len(groups) != expected_groups or targets.keys() & groups.keys():
            raise ValueError('Unexpected or overlapping container groups.')
        names, notes[source] = gap.source_names(root, details['datasets'][source]['input'], set(groups))
        for entity_id, item in groups.items():
            item['saved_names'] = names.get(entity_id, [])
            item['saved_effort_hours'] = str(item['saved_effort_hours'])
        targets.update(groups)
    context = shared.prepare(root, root / ACCESS_COMPARISON)
    if context['control'] in targets or gap.sha(path) != digest:
        raise ValueError('Control overlap or changed report.')
    return dict(report=str(report), details_sha256=digest, difference_input=audit,
        targets=targets, control=context['control'], proxies=context['proxies'],
        proxy_source=context['source_audit'], name_notes=notes)


def interpretation(a, b):
    if 'NOT_CHECKED' in (a, b) or 'UNRESOLVED' in (a, b):
        return 'INCOMPLETE_CHECK'
    if a == b == 'RETRIEVED':
        return 'RETRIEVED_WITH_BOTH'
    if a == 'RETRIEVED':
        return 'RETRIEVED_WITH_SURYA_ONLY_THIS_CHECK'
    if b == 'RETRIEVED':
        return 'RETRIEVED_WITH_AKASH_ONLY_THIS_CHECK'
    return 'NOT_RETRIEVED_WITH_EITHER_THIS_CHECK'


def hidden_token(label):
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        token = getpass.getpass(label + ' Wrike token (hidden): ').strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError('Empty token or whitespace in token.')
    return token


def execute(root, context, token_reader=hidden_token, session_factory=requests.Session):
    out = root / 'output/effort_token_comparison' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    out.mkdir(parents=True, exist_ok=False)
    report = {k: v for k, v in context.items() if k != 'proxies'}
    report.update(started_at_utc=shared.utc_now(), status='RUNNING', attempts=[],
        token_identity='Labels supplied by operator; identities not independently verified.',
        limitation='Current container lookups only. Not proof of historical permission changes, deletion, task completeness, or current effort totals.',
        controls={}, results={label: {i: {'outcome': 'NOT_CHECKED'} for i in context['targets']}
                              for label in ('Surya', 'Akash')})

    def save():
        temporary = out / 'access_comparison.json.partial'
        temporary.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        temporary.replace(out / 'access_comparison.json')

    previous_digest = None
    save()
    try:
        for label in ('Surya', 'Akash'):
            print('\nNext: ' + label + "'s token. One control and six containers.", flush=True)
            token = token_reader(label)
            digest = hashlib.sha256(token.encode()).digest()
            if digest == previous_digest:
                raise ValueError('The same token was entered twice. Akash checks were not started.')
            previous_digest = digest
            session = session_factory()
            def emit(role, entity_id, attempt, status, reason, delay):
                report['attempts'].append(dict(token_label=label, role=role, container_id=entity_id,
                    attempt=attempt, http_status=status, reason=reason, retry_wait_seconds=delay,
                    checked_at_utc=shared.utc_now()))
                save()
            try:
                probe = shared.Probe(session, token, context['proxies'], emit)
                result, stop = probe.lookup(context['control'], 'shared_control')
                report['controls'][label] = result
                save()
                print(label + ' control:', result['outcome'], result['http_status'], flush=True)
                if stop or result['outcome'] != 'RETRIEVED':
                    report['status'] = 'STOPPED_' + label.upper() + '_CONTROL'
                    break
                for entity_id, item in context['targets'].items():
                    result, stop = probe.lookup(entity_id, 'effort_gap_container')
                    report['results'][label][entity_id] = result
                    save()
                    print(label + ' | ' + (' / '.join(item['saved_names']) or entity_id)
                          + ' | ' + result['outcome'] + ' | HTTP ' + str(result['http_status']), flush=True)
                    if stop:
                        report['status'] = 'STOPPED_' + label.upper() + '_REQUEST_ERROR'
                        break
                if stop:
                    break
            finally:
                session.close()
                token = None
                if 'probe' in locals():
                    probe.token = None
        else:
            report['status'] = 'CHECKS_FINISHED'
    except KeyboardInterrupt:
        report['status'] = 'INTERRUPTED'
    except Exception:
        report['status'] = 'STOPPED_LOCAL_ERROR'
        raise
    finally:
        report['finished_at_utc'] = shared.utc_now()
        save()
        print('\nSIX-CONTAINER COMPARISON: ' + report['status'])
        with (out / 'container_access_comparison.csv').open('x', encoding='utf-8-sig', newline='') as handle:
            fields = ['container_id', 'saved_name', 'saved_difference_type', 'saved_task_count',
                'saved_effort_hours', 'surya_outcome', 'akash_outcome', 'comparison']
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for entity_id, item in context['targets'].items():
                a, b = (report['results'][label][entity_id]['outcome'] for label in ('Surya', 'Akash'))
                writer.writerow(dict(container_id=entity_id, saved_name=' / '.join(item['saved_names']),
                    saved_difference_type=item['saved_difference_type'], saved_task_count=item['saved_task_count'],
                    saved_effort_hours=item['saved_effort_hours'], surya_outcome=a, akash_outcome=b,
                    comparison=interpretation(a, b)))
                print((' / '.join(item['saved_names']) or entity_id) + ': Surya=' + a + '; Akash=' + b)
        print('Saved:', out)
        print(report['limitation'])
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--check-inputs', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    context = prepare(root, args.report.expanduser().resolve() if args.report else None)
    print('Verified 6 containers: 172 Surya-only tasks / 22,845.50 h; 635 Akash-only tasks / 43,432 h.')
    if args.check_inputs:
        print('Offline validation complete; no API calls made.')
        return
    print('Direct folder lookups only. No full extraction and no external writes.')
    execute(root, context)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, csv.Error, getpass.GetPassWarning) as error:
        print('STOPPED: ' + str(error), file=sys.stderr)
        sys.exit(1)
