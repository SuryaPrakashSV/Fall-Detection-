#!/usr/bin/env python3
"""Offline explanation of remaining nonproject task scope and changed missing projects.
Reads saved evidence only. No API, token prompts or changes to source/workbook.
"""
import argparse
import csv
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path

getcontext().prec = 60
HISTORICAL_HOUR_PRECISION=Decimal('0.0000000001')
csv.field_size_limit(sys.maxsize)
FOLDERS = ('parent_folder_id','child_folder_id','grandchild_folder_id','baby_folder_id','grandbaby_folder_id')
TASKS = ('parent_task_id','child_task_id','grandchild_task_id','baby_task_id','grandbaby_task_id','great_grandbaby_task_id')
NULLS = {'','nan','none','null','nat','<na>','placeholder','\\n'}


def check(ok, message):
    if not ok: raise ValueError(message)


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1048576),b''): h.update(block)
    return h.hexdigest()


def load(path):
    with Path(path).open(encoding='utf-8-sig') as f:return json.load(f)


def ident(v):
    v=(v or '').strip()
    return '' if v.lower() in NULLS else v


def number(v):
    try:
        result=Decimal(str(v))
        check(result.is_finite() and result>=0,'Unknown or invalid effort in closeout scope')
        return result
    except (InvalidOperation,TypeError):raise ValueError('Unknown or invalid effort in closeout scope')


def comparison_hours(v):
    return number(v).quantize(HISTORICAL_HOUR_PRECISION)


def read_rows(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as f:
        r=csv.DictReader(f,strict=True)
        headers=[h.strip().lower() for h in (r.fieldnames or [])]
        check(headers and len(headers)==len(set(headers)),'Invalid headers: '+str(path))
        r.fieldnames=headers
        for n,row in enumerate(r,2):
            check(None not in row and all(v is not None for v in row.values()),'Malformed CSV row '+str(n))
            yield n,row


def write_csv(path, records, fields):
    with Path(path).open('x',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields)
        writer.writeheader();writer.writerows(records)


def verify_outputs(directory):
    manifest=load(directory/'output_hashes.json')
    needed={'analysis_summary.json','task_audit.csv','project_effort.csv','other_containers.csv'}
    check(needed<=set(manifest),'Required supporting files are not hash-covered')
    for name,digest in manifest.items():
        p=Path(name)
        check(not p.is_absolute() and '..' not in p.parts,'Unsafe supporting file path')
        resolved=(directory/p).resolve()
        check(resolved.is_relative_to(directory.resolve()) and resolved.is_file(),'Missing or unsafe supporting file')
        check(sha(resolved)==digest,'Supporting file hash mismatch: '+name)


def run(root, snapshot, workbook_dir=None):
    root=Path(root).resolve();snapshot=Path(snapshot).resolve()
    snapshot_hash=sha(snapshot)
    if workbook_dir:
        candidates=[Path(workbook_dir).resolve()/'workbook_receipt.json']
    else:
        candidates=[]
        for p in (root/'output'/'holistic_workbook').glob('*/workbook_receipt.json'):
            r=load(p)
            if r.get('status')=='WORKBOOK_CREATED_REVIEW_REQUIRED' and r.get('source',{}).get('snapshot_sha256')==snapshot_hash:
                candidates.append(p)
    check(len(candidates)==1,'Expected one matching completed workbook. Specify --workbook-dir if there are several.')
    receipt_path=candidates[0];receipt=load(receipt_path);directory=receipt_path.parent
    check(receipt.get('status')=='WORKBOOK_CREATED_REVIEW_REQUIRED','Workbook receipt is incomplete')
    check(receipt['source']['snapshot_sha256']==snapshot_hash,'Snapshot does not match workbook receipt')
    workbook=directory/Path(receipt['workbook']).name
    check(workbook.is_file() and sha(workbook)==receipt['workbook_sha256'],'Workbook changed since receipt; use the saved original')
    support=directory/'supporting_calculations';verify_outputs(support)
    analysis=load(support/'analysis_summary.json')
    check(analysis['metadata']['snapshot_sha256']==snapshot_hash,'Supporting analysis snapshot differs')
    check(analysis['summary']==receipt['summary'],'Workbook and supporting summaries differ')
    projects={r['project_id']:r for _,r in read_rows(support/'project_effort.csv')}
    containers={r['container_id']:r for _,r in read_rows(support/'other_containers.csv')}
    check(not set(projects)&set(containers),'Project/other container records overlap')
    containers.update(projects)
    neither={}
    for _,r in read_rows(support/'task_audit.csv'):
        if r['partition']!='NEITHER':continue
        tid=r['task_id'];check(tid not in neither,'Duplicate NEITHER task ID')
        source_rows={int(v) for v in r['source_excel_rows'].split(';')}
        check(source_rows and min(source_rows)>=2,'Invalid source row evidence')
        neither[tid]={'minutes':number(r['effort_minutes']),'expected_rows':source_rows,
                      'seen_rows':set(),'containers':set(),'project_count':int(r['all_project_count'])}
    recon={r['metric_id']:r for r in analysis['reconciliation']}
    expected_minutes=number(recon['NEITHER']['effort_minutes'])
    check(sum((r['minutes'] for r in neither.values()),Decimal(0))==expected_minutes,'NEITHER audit effort differs from reconciliation')
    check(len(neither)==recon['NEITHER']['task_count'],'NEITHER task count differs from reconciliation')
    required={'key','id','effortallocation_totaleffort',*FOLDERS,*TASKS}
    count=0
    for n,row in read_rows(snapshot):
        if count==0:check(required<=set(row),'Snapshot identity/hierarchy columns missing')
        count+=1;key=ident(row['key'])
        if key not in neither:continue
        t=neither[key]
        if n not in t['expected_rows']:continue
        path=[ident(row[c]) for c in TASKS]
        deepest=next((x for x in reversed(path) if x),'')
        scopes={ident(row[c]) for c in FOLDERS}|{ident(row['id'])};scopes.discard('')
        check(key==deepest and key not in scopes,'Source task identity no longer matches task audit')
        check(number(row['effortallocation_totaleffort'])==t['minutes'],'Source effort differs from task audit')
        t['seen_rows'].add(n);t['containers'].update(scopes)
    check(count==receipt['summary']['snapshot_data_rows'],'Snapshot row count differs')
    check(sha(snapshot)==snapshot_hash,'Snapshot changed while reading')
    groups=defaultdict(lambda:{'task_count':0,'minutes':Decimal(0)})
    task_container_rows=[];flags=[]
    for tid,t in sorted(neither.items()):
        check(t['seen_rows']==t['expected_rows'],'Missing recorded source rows for task '+tid)
        ids=tuple(sorted(t['containers']))
        groups[ids]['task_count']+=1;groups[ids]['minutes']+=t['minutes']
        if t['project_count']!=0 or any(containers.get(c,{}).get('current_type')=='PROJECT' for c in ids):
            flags.append({'task_id':tid,'issue':'NEITHER_HAS_PROJECT_ASSOCIATION'})
        if any(c not in containers for c in ids):flags.append({'task_id':tid,'issue':'UNKNOWN_CONTAINER_ID'})
        task_container_rows.append({'task_id':tid,'effort_minutes':t['minutes'],'effort_hours':t['minutes']/60,
                                    'container_ids':';'.join(ids),'source_excel_rows':';'.join(map(str,sorted(t['seen_rows'])))})
    grouped=[]
    for ids,g in groups.items():
        details=[f"{c} | {containers.get(c,{}).get('name','UNKNOWN')} | {containers.get(c,{}).get('current_type','UNKNOWN')} | {containers.get(c,{}).get('akash_access','UNKNOWN')}" for c in ids]
        grouped.append({'container_ids':';'.join(ids),'container_details':' || '.join(details) or '(no recorded containers)',
                        'task_count':g['task_count'],'effort_minutes':g['minutes'],'effort_hours':g['minutes']/60})
    grouped.sort(key=lambda r:(-r['effort_minutes'],r['container_ids']))
    check(sum((g['effort_minutes'] for g in grouped),Decimal(0))==expected_minutes,'Scope group arithmetic failed')
    historical=root/'output'/'effort_reconciliation'/'20260929T172437_318678Z'/'missing_projects_effort_hours.csv'
    current={pid:r for pid,r in projects.items() if r['access_group']=='MISSING' and r['in_frozen_snapshot']=='1'}
    changes=[];comparison={'status':'HISTORICAL_REPORT_UNAVAILABLE','path':str(historical)}
    if historical.is_file():
        old={}
        for _,r in read_rows(historical):
            check({'project_id','project_name','is_confirmed_project_in_saved_baseline','effort_hours'}<=set(r),'Unexpected historical report schema')
            pid=ident(r['project_id']);check(pid and pid not in old,'Historical report has invalid/duplicate IDs')
            check(number(r['is_confirmed_project_in_saved_baseline'])==1,'Historical report contains a nonproject record')
            old[pid]=r
        check(len(old)==56,'Historical report does not contain the expected 56 confirmed projects')
        added=set(current)-set(old);removed=set(old)-set(current);shared=set(old)&set(current)
        comparison={'status':'COMPARED','path':str(historical),'sha256':sha(historical),
                    'historical_project_count':len(old),'current_project_count':len(current),
                    'added_count':len(added),'removed_count':len(removed),'shared_count':len(shared)}
        for change,ids in (('ADDED',added),('REMOVED',removed),('SHARED',shared)):
            for pid in sorted(ids):
                now=projects.get(pid) or containers.get(pid,{})
                before=old.get(pid,{}).get('effort_hours','');after=now.get('effort_hours','')
                delta=comparison_hours(after)-comparison_hours(before) if before!='' and after!='' else None
                label=change if change!='SHARED' else 'SHARED_UNKNOWN' if delta is None else 'SHARED_CHANGED' if delta else 'SHARED_UNCHANGED'
                changes.append({'change':label,'project_id':pid,'name':now.get('project_name') or now.get('name') or old.get(pid,{}).get('project_name',''),
                                'historical_effort_hours':before,'current_effort_hours':after,'effort_hours_change':delta,
                                'current_type':now.get('current_type','NOT_IN_CURRENT_REVIEW'),
                                'current_access':now.get('akash_access','NOT_IN_CURRENT_REVIEW')})
        comparison['shared_changed_count']=sum(r['change']=='SHARED_CHANGED' for r in changes)
        comparison['shared_unknown_count']=sum(r['change']=='SHARED_UNKNOWN' for r in changes)
        comparison['comparison_precision_note']='Per-project hours rounded to 10 decimal places to match the historical report precision.'
        if (all(r.get('effort_hours','')!='' for r in old.values())
                and all(r.get('effort_hours','')!='' for r in current.values())):
            old_sum=sum((comparison_hours(r['effort_hours']) for r in old.values()),Decimal(0))
            current_sum=sum((comparison_hours(r['effort_hours']) for r in current.values()),Decimal(0))
            added_sum=sum((comparison_hours(current[p]['effort_hours']) for p in added),Decimal(0))
            removed_sum=sum((comparison_hours(old[p]['effort_hours']) for p in removed),Decimal(0))
            shared_delta=sum((comparison_hours(current[p]['effort_hours'])-comparison_hours(old[p]['effort_hours']) for p in shared),Decimal(0))
            check(current_sum==old_sum+added_sum-removed_sum+shared_delta,'Historical row-sum bridge failed')
            comparison['project_row_sum_bridge']={'historical_hours':old_sum,'current_hours':current_sum,
                'added_hours':added_sum,'removed_hours':removed_sum,'shared_project_delta_hours':shared_delta,
                'residual_hours':Decimal(0),'note':'Sum of project rows rounded to 10 decimals each; shared tasks may repeat. Not a deduplicated task-union bridge.'}
    api_only=[{'project_id':pid,'project_name':r['project_name'],'snapshot_effort':'UNKNOWN_NOT_IN_SNAPSHOT'}
              for pid,r in sorted(projects.items()) if r['in_frozen_snapshot']=='0']
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    output=directory/'closeout'/stamp;output.mkdir(parents=True,exist_ok=False)
    summary={'status':'OFFLINE_CLOSEOUT_CHECK_COMPLETE_REVIEW_REQUIRED','created_at':datetime.now(timezone.utc).isoformat(),
             'snapshot_sha256':snapshot_hash,'workbook_sha256':receipt['workbook_sha256'],
             'receipt_sha256':sha(receipt_path),'helper_sha256':sha(Path(__file__)),
             'neither_task_count':len(neither),'neither_effort_minutes':expected_minutes,'neither_effort_hours':expected_minutes/60,
             'disjoint_scope_group_count':len(grouped),'scope_flags':flags,'missing_project_comparison':comparison,
             'api_only_projects':api_only,
             'notes':['Groups use each task\'s complete set of recorded container IDs and are disjoint.',
                      'NEITHER means no membership in the analyzed accessible/missing project unions; it is not missing task access.',
                      'No project association in exported hierarchy does not prove no project exists in Wrike.',
                      'Added/removed describe differences between dated missing-project lists, not proven permission changes.']}
    write_csv(output/'neither_scope_groups.csv',grouped,['container_ids','container_details','task_count','effort_minutes','effort_hours'])
    write_csv(output/'neither_task_containers.csv',task_container_rows,['task_id','effort_minutes','effort_hours','container_ids','source_excel_rows'])
    write_csv(output/'missing_project_changes.csv',changes,['change','project_id','name','historical_effort_hours','current_effort_hours','effort_hours_change','current_type','current_access'])
    write_csv(output/'api_only_projects.csv',api_only,['project_id','project_name','snapshot_effort'])
    with (output/'summary.json').open('x',encoding='utf-8') as f:json.dump(summary,f,indent=2,default=str)
    lines=[f"NEITHER tasks: {len(neither):,}",f"NEITHER hours: {expected_minutes/60:,.6f}",
           f"Disjoint stored-container groups: {len(grouped)}",f"Scope flags: {len(flags)}",'Largest groups:']
    top_limit=6 if api_only else 10
    def short(value,limit=200):
        value=str(value).replace('\n',' ').replace('\r',' ')
        return value if len(value)<=limit else value[:limit]+'...'
    for g in grouped[:top_limit]:lines.append(f"{g['task_count']:,} tasks | {g['effort_hours']:,.6f} h | {short(g['container_details'])}")
    if len(grouped)>top_limit:lines.append(f"Other {len(grouped)-top_limit} groups: {sum((g['effort_hours'] for g in grouped[top_limit:]),Decimal(0)):,.6f} h")
    lines.append('Historical missing-project comparison: '+comparison['status'])
    if comparison['status']=='COMPARED':
        lines.append(f"Added: {comparison['added_count']}; removed: {comparison['removed_count']}; shared: {comparison['shared_count']}; shared changed: {comparison['shared_changed_count']}")
        bridge=comparison.get('project_row_sum_bridge')
        if bridge:lines.append(f"Project row sum (10dp comparison): {bridge['historical_hours']:,.6f} + added {bridge['added_hours']:,.6f} - removed {bridge['removed_hours']:,.6f} + shared changes {bridge['shared_project_delta_hours']:,.6f} = {bridge['current_hours']:,.6f} h")
        changed=[r for r in changes if r['change']!='SHARED_UNCHANGED']
        for r in changed[:4]:lines.append(f"{r['change']} | {r['project_id']} | {short(r['name'],80)} | current hours {r['current_effort_hours'] or 'unavailable'}")
        if len(changed)>4:lines.append(f"{len(changed)-4} additional changed/unknown records saved in CSV")
    else:lines.append('Expected historical report: '+str(historical))
    lines.append(f'API-only projects: {len(api_only)}; snapshot effort UNKNOWN')
    for r in api_only[:5]:lines.append(r['project_id']+' | '+short(r['project_name'],80)+' | UNKNOWN')
    lines+=summary['notes'];lines.append('OUTPUT: '+str(output))
    text='\n'.join(lines)
    (output/'summary.txt').write_text(text+'\n',encoding='utf-8')
    with (output/'output_hashes.json').open('x',encoding='utf-8') as f:
        json.dump({p.name:sha(p) for p in output.iterdir() if p.is_file() and p.name!='output_hashes.json'},f,indent=2)
    print(text)
    return summary,grouped,changes,output


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--snapshot',default='snowflake_snapshot.csv');p.add_argument('--workbook-dir')
    args=p.parse_args();run(Path.cwd(),args.snapshot,args.workbook_dir)


if __name__=='__main__':
    try:main()
    except (ValueError,OSError,KeyError,csv.Error) as e:raise SystemExit('CLOSEOUT STOPPED: '+str(e))
