#!/usr/bin/env python3
"""Read-only direct task lookups for the 196 additional Akash-only exceptions.
Two token labels are operator supplied, not independently identified.
Requires existing review_wrike_effort_gaps.py and check_missing_wrike_ids.py.
No source edits, database writes, full extraction, or stored credentials.
"""
import argparse
import csv
import getpass
import hashlib
import json
import re
import sys
import time
import warnings
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote
import requests
import check_missing_wrike_ids as shared
import review_wrike_effort_gaps as gap


def task_response(response):
    try:
        body = response.json()
    except ValueError:
        return 'UNRESOLVED', 'NON_JSON_RESPONSE', {}
    if not isinstance(body, dict):
        return 'UNRESOLVED', 'UNEXPECTED_JSON_SHAPE', {}
    if response.status_code == 200:
        data = body.get('data')
        if body.get('kind') != 'tasks' or not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            return 'UNRESOLVED', 'UNEXPECTED_TASK_RESPONSE', {}
        return 'RETRIEVED', 'RETURNED_BY_DIRECT_TASK_LOOKUP', data[0]
    if response.status_code in (403,404):
        if not isinstance(body.get('error'), str) or not body['error'].strip():
            return 'UNRESOLVED', 'NON_API_ERROR_RESPONSE', {}
        return ('FORBIDDEN' if response.status_code == 403 else 'NOT_FOUND'), 'WRIKE_HTTP_'+str(response.status_code), {}
    return 'UNRESOLVED', 'UNEXPECTED_HTTP_STATUS', {}


class TaskProbe:
    def __init__(self, session, token, proxies, emit, sleep=time.sleep, monotonic=time.monotonic):
        self.session, self.token, self.proxies, self.emit = session, token, proxies, emit
        self.sleep, self.monotonic, self.next_request = sleep, monotonic, 0.0
        self.current_attempt = 0

    def lookup(self, entity_id, role):
        shared.check_id(entity_id)
        self.current_attempt = 0
        for attempt in range(1, shared.MAX_ATTEMPTS + 1):
            wait = max(0.0, self.next_request - self.monotonic())
            if wait:
                self.sleep(wait)
            self.current_attempt = attempt
            self.next_request = self.monotonic() + 1.0
            status, payload, stop = "", {}, False
            try:
                response = self.session.get(
                    "https://www.wrike.com/api/v4/tasks/" + quote(entity_id, safe=""),
                    headers={"Authorization": "Bearer " + self.token},
                    proxies=self.proxies, timeout=(15, 60), allow_redirects=False,
                )
            except KeyboardInterrupt:
                self.emit(role, entity_id, attempt, "", "INTERRUPTED", 0)
                raise
            except requests.RequestException:
                reason, retry, delay = "NETWORK_REQUEST_ERROR", True, float(2 ** (attempt - 1))
            else:
                status = response.status_code
                if status == 401:
                    outcome, reason, retry, delay, stop = "UNRESOLVED", "AUTHENTICATION_FAILED", False, 0, True
                elif status in shared.RETRY_HTTP:
                    retry = True
                    reason = "RATE_LIMITED" if status == 429 else "RETRYABLE_SERVER_ERROR"
                    delay = shared.retry_wait(response.headers.get("Retry-After"), 5.0 if status == 429 else float(2 ** (attempt - 1)))
                else:
                    outcome, reason, payload = task_response(response)
                    retry, delay = False, 0
                    if outcome == "RETRIEVED" and payload.get("id") != entity_id:
                        outcome, reason, payload = "UNRESOLVED", "RETURNED_ID_MISMATCH", {}
            if retry:
                if delay > shared.MAX_WAIT:
                    outcome, reason, stop, retry = "UNRESOLVED", "RETRY_AFTER_EXCEEDS_WAIT_LIMIT", True, False
                elif attempt == shared.MAX_ATTEMPTS:
                    outcome, reason, stop, retry = "UNRESOLVED", reason + "_RETRIES_EXHAUSTED", True, False
            self.emit(role, entity_id, attempt, status, reason, delay if retry else 0)
            if retry:
                print("Retry {}/{} for {} after {:.1f}s ({})".format(attempt, shared.MAX_ATTEMPTS, entity_id, delay, reason), flush=True)
                self.next_request = max(self.next_request, self.monotonic() + delay)
                continue
            return {
                "outcome": outcome, "reason": reason, "http_status": status,
                "checked_at_utc": shared.utc_now(), "attempts": attempt,
                "api_title": payload.get("title", ""),
                "api_status": payload.get("status", ""),
                "api_createdDate": payload.get("createdDate", ""),
                "api_updatedDate": payload.get("updatedDate", ""),
                "api_parentIds": payload.get("parentIds", []),
                "api_superTaskIds": payload.get("superTaskIds", []),
                "api_account_id": payload.get("accountId", ""), "api_scope": payload.get("scope", ""),
            }, stop
        raise RuntimeError("Lookup ended without a recorded outcome.")


def prepare(root, report=None):
    if report is None:
        matches=sorted((root/'output/effort_reconciliation').glob('*/effort_reconciliation_details.json'))
        if not matches: raise ValueError('No saved effort report found.')
        report=matches[-1].parent
    path=report/'effort_reconciliation_details.json'
    digest=gap.sha(path)
    details=json.loads(path.read_text(encoding='utf-8-sig'))
    data,audits={},{}
    for label in (gap.OLD,gap.NEW):
        comparison=details['comparisons'][label]
        name=comparison['task_difference_file']
        if Path(name).name!=name: raise ValueError('Unexpected difference filename.')
        data[label],audits[label]=gap.read_differences(report/name,comparison)
    old={r['task_id'] for r in data[gap.OLD]}
    rows=[r for r in data[gap.NEW] if r['difference_type']=='AKASH_ONLY' and r['task_id'] not in old]
    if len(rows)!=196: raise ValueError('Expected 196 additional Akash-only task IDs.')
    gap.same(gap.total(rows,'_b'),'4771','Saved effort for 196 tasks')
    controls=sorted(r['task_id'] for r in data[gap.NEW] if r['difference_type']=='SHARED_EFFORT_CHANGED')
    if not controls: raise ValueError('No shared task available as a positive control.')
    targets={r['task_id']:dict(saved_hours=str(r['_b']),saved_titles=r['_akash_titles'],
             saved_container_ids=r['_akash_direct_container_ids']) for r in rows}
    if len(targets)!=196 or controls[0] in targets: raise ValueError('Duplicate or overlapping IDs.')
    for value in [*targets,controls[0]]:
        if not re.fullmatch(r'[A-Za-z0-9_-]+',value): raise ValueError('Unexpected ID format.')
    proxies,proxy_audit=shared.read_proxies(root/'Wrike_Data_local_validation.py')
    if gap.sha(path)!=digest: raise ValueError('Saved report changed during input checks.')
    return dict(targets=targets,control=controls[0],proxies=proxies,proxy_source=proxy_audit,
                report=str(report),details_sha256=digest,difference_inputs=audits)


def read_token(label):
    with warnings.catch_warnings():
        warnings.simplefilter('error',getpass.GetPassWarning)
        token=getpass.getpass(label+' Wrike token (hidden): ').strip()
    if not token or any(c.isspace() for c in token): raise ValueError('Invalid token input.')
    return token


def execute(root,context,token_reader=read_token,session_factory=requests.Session):
    out=root/'output/remaining_196_access'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    out.mkdir(parents=True,exist_ok=False)
    report={k:v for k,v in context.items() if k!='proxies'}
    report.update(started_at_utc=shared.utc_now(),status='RUNNING',attempts=[],controls={},
        token_identity='Operator supplied labels; identities not independently verified.',
        limitation='Current direct task lookups; NOT_FOUND is not proof of deletion. Saved hours are not current effort. Retrieval does not establish why a task is absent from the table.',
        results={label:{i:{'outcome':'NOT_CHECKED'} for i in context['targets']} for label in ('Surya','Akash')})
    def save():
        temp=out/'task_access_results.json.partial'
        temp.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
        temp.replace(out/'task_access_results.json')
    previous_digest=None
    save()
    try:
        for label in ('Surya','Akash'):
            print('\nNext: '+label+' token. One shared task control, then 196 task IDs.',flush=True)
            token=token_reader(label)
            digest=hashlib.sha256(token.encode()).digest()
            if digest==previous_digest: raise ValueError('Same token entered twice; second check not started.')
            previous_digest=digest
            session=session_factory()
            def emit(role,task_id,attempt,status,reason,delay):
                report['attempts'].append(dict(token_label=label,role=role,task_id=task_id,
                    attempt=attempt,http_status=status,reason=reason,retry_wait_seconds=delay,checked_at_utc=shared.utc_now()))
                save()
            try:
                probe=TaskProbe(session,token,context['proxies'],emit)
                result,stop=probe.lookup(context['control'],'SHARED_TASK_CONTROL')
                report['controls'][label]=result
                save()
                print(label+' control:',result['outcome'],result['http_status'],flush=True)
                if stop or result['outcome']!='RETRIEVED':
                    report['status']='STOPPED_'+label.upper()+'_CONTROL'
                    break
                for index,task_id in enumerate(context['targets'],1):
                    result,stop=probe.lookup(task_id,'REMAINING_TASK')
                    report['results'][label][task_id]=result
                    save()
                    print(f'{label} {index}/196: {task_id} {result["outcome"]}',flush=True)
                    if stop:
                        report['status']='STOPPED_'+label.upper()+'_REQUEST_ERROR'
                        break
                if stop: break
            finally:
                session.close()
                token=None
                if 'probe' in locals(): probe.token=None
        else: report['status']='CHECKS_FINISHED'
    except KeyboardInterrupt:
        report['status']='INTERRUPTED'
    except Exception:
        report['status']='STOPPED_LOCAL_ERROR'
        raise
    finally:
        report['finished_at_utc']=shared.utc_now()
        save()
        buckets=defaultdict(lambda:[0,Decimal(0)])
        for task_id,item in context['targets'].items():
            pair=tuple(report['results'][label][task_id]['outcome'] for label in ('Surya','Akash'))
            buckets[pair][0]+=1
            buckets[pair][1]+=Decimal(item['saved_hours'])
        print('\nREMAINING 196 TASK SUMMARY: '+report['status'])
        with (out/'task_access_summary.csv').open('x',encoding='utf-8-sig',newline='') as handle:
            writer=csv.writer(handle)
            writer.writerow(['surya_outcome','akash_outcome','task_count','saved_akash_hours'])
            for (a,b),(count,hours) in sorted(buckets.items()):
                writer.writerow([a,b,count,str(hours)])
                print(f'Surya={a}; Akash={b}; tasks={count}; saved hours={hours}')
        print('Saved:',out)
        print(report['limitation'])
    return out


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path)
    parser.add_argument('--check-inputs',action='store_true')
    args=parser.parse_args()
    root=Path(__file__).resolve().parent
    context=prepare(root,args.report.expanduser().resolve() if args.report else None)
    print('Verified: 196 task IDs, 4,771 saved hours; one shared task control.')
    if args.check_inputs:
        print('Offline checks complete; no API calls.')
        return
    print('394 direct GETs before retries, one per second. No full extraction or external writes.')
    execute(root,context)


if __name__=='__main__':
    try: main()
    except (ValueError,KeyError,OSError,csv.Error,getpass.GetPassWarning) as exc:
        print('STOPPED: '+str(exc),file=sys.stderr)
        sys.exit(1)
