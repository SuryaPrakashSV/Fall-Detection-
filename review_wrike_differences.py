#!/usr/bin/env python3
"""Read saved comparison evidence and summarize unresolved Wrike differences.
No API calls, credentials or source execution. Inputs are never modified.
Writes a new review directory and prints a bounded summary.
"""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re


def load(path): return json.loads(path.read_text(encoding='utf-8-sig'))
def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()
def verify(path, expected):
    if not expected or sha(path)!=expected: raise ValueError('Evidence hash mismatch: '+str(path))
def dt(value):
    d=datetime.fromisoformat(value.replace('Z','+00:00'))
    if d.tzinfo is None: raise ValueError('Timestamp has no timezone')
    return d

def selected_reference(directory, wanted, manifest_hash):
    verify(directory/'manifest.json',manifest_hash)
    manifest=load(directory/'manifest.json')
    verify(directory/'tasks.jsonl',manifest['files']['tasks.jsonl'])
    result={}
    with (directory/'tasks.jsonl').open(encoding='utf-8') as f:
        for line in f:
            row=json.loads(line)
            if row['task_id'] in wanted: result[row['task_id']]=row
    return result

def creation_bucket(record, timing):
    dates={v['task'].get('createdDate') for v in record['variants']}
    if None in dates or len(dates)!=1: return 'UNKNOWN_OR_CONFLICTING_CREATED_DATE'
    try: created=dt(next(iter(dates)))
    except (ValueError,TypeError): return 'INVALID_CREATED_DATE'
    if created<=dt(timing['before_finished_utc']): return 'CREATED_BY_BEFORE_CAPTURE_END'
    if created<dt(timing['extractor_started_utc']): return 'CREATED_BETWEEN_BEFORE_AND_EXTRACTION'
    if created<=dt(timing['extractor_finished_utc']): return 'CREATED_DURING_EXTRACTION_WINDOW'
    if created<dt(timing['after_started_utc']): return 'CREATED_BETWEEN_EXTRACTION_AND_AFTER'
    return 'CREATED_DURING_OR_AFTER_AFTER_START'

def redact(line):
    if re.search(r'authorization|bearer|password|secret|token\s*[:=]',line,re.I):
        return '[credential-related line omitted]'
    line=re.sub(r'(https?://[^\s?]+)\?[^\s]+',r'\1?[query omitted]',line)
    return line.strip()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--case',required=True,type=Path)
    args=p.parse_args(); case=args.case.resolve()
    candidates=[p for p in case.glob('offline_comparison_*/summary.json')
                if load(p).get('status') in {'COMPARISON_COMPLETE_REVIEW_REQUIRED','OBSERVED_TASK_EFFORT_MATCH'}]
    if len(candidates)!=1: raise ValueError('Expected one completed comparison; found '+str(len(candidates)))
    summary_path=candidates[0]; comparison=summary_path.parent
    output_hashes=load(comparison/'output_hashes.json')
    for name in ('summary.json','task_discrepancies.csv','extraction_log_review.csv'):
        verify(comparison/name,output_hashes[name])
    summary=load(summary_path); provenance=summary['provenance']; timing=summary['timing']
    with (comparison/'task_discrepancies.csv').open(newline='') as f: rows=list(csv.DictReader(f))
    wanted={r['task_id'] for r in rows}
    before=selected_reference(Path(provenance['before_path']),wanted,provenance['before_manifest_sha256'])
    after=selected_reference(Path(provenance['after_path']),wanted,provenance['after_manifest_sha256'])
    receipt_path=Path(provenance['receipt_path']);verify(receipt_path,provenance['receipt_sha256'])
    receipt=load(receipt_path);log_path=receipt_path.parent/'extractor.log'
    verify(log_path,receipt['extractor_log_sha256'])
    verify(case/'frozen_scope.json',receipt['frozen_scope_sha256'])
    frozen=load(case/'frozen_scope.json'); titles=frozen.get('root_titles',{})
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    out=case/('difference_review_'+stamp);out.mkdir(exist_ok=False)
    details=[]; buckets={}; extra_csv=[]; membership=[]; field_changes=[]
    for row in rows:
        tid=row['task_id']; flags=set(row['review_flags'].split(';'))
        a=after.get(tid); b=before.get(tid)
        record={'task_id':tid,'comparison':row,'before':b,'after':a}
        if row['after_comparison']=='REFERENCE_ONLY':
            bucket=creation_bucket(a,timing)
            group='LINKED_TO_ADDED_OR_REMOVED_SCOPE_FOLDER' if row['changed_scope_folder_ids'] else 'OUTSIDE_CHANGED_SCOPE_FOLDERS'
            key=(group,bucket)
            item=buckets.setdefault(key,{'task_count':0,'explicit_minutes':Decimal(0),'nonnumeric_tasks':0})
            item['task_count']+=1
            if row['after_minutes']!='': item['explicit_minutes']+=Decimal(row['after_minutes'])
            else: item['nonnumeric_tasks']+=1
            record['creation_bucket']=bucket;record['folder_group']=group
        if row['before_comparison']=='CSV_ONLY':
            extra_csv.append({'task_id':tid,'csv_minutes':row['csv_minutes'],
                'after_minutes':row['after_minutes'],'created_dates':sorted({v['task'].get('createdDate','') for v in a['variants']}) if a else []})
        if 'CSV_AFTER_ROOT_MEMBERSHIP_DIFFERS' in flags or 'CSV_BEFORE_ROOT_MEMBERSHIP_DIFFERS' in flags:
            item={'task_id':tid}
            for label in ('csv','before','after'):
                ids=[x for x in row[label+'_roots'].split(';') if x]
                item[label+'_roots']=[{'id':x,'title':titles.get(x,'')} for x in ids]
            membership.append(item)
        if 'REFERENCE_TASK_CHANGED' in flags:
            if a and b and len(a['variants'])==len(b['variants'])==1:
                old=b['variants'][0]['task'];new=a['variants'][0]['task']
                changed={k:{'before':old.get(k),'after':new.get(k)} for k in old.keys()|new.keys() if old.get(k)!=new.get(k)}
                field_changes.append({'task_id':tid,'changes':changed})
            else: field_changes.append({'task_id':tid,'changes':'conflicting variants require review'})
        details.append(record)
    bucket_summary=[]
    for (group,bucket),v in sorted(buckets.items()):
        bucket_summary.append({'folder_group':group,'creation_bucket':bucket,'task_count':v['task_count'],
                               'explicit_hours':str(v['explicit_minutes']/60),'nonnumeric_tasks':v['nonnumeric_tasks']})
    with (comparison/'extraction_log_review.csv').open(newline='') as f:
        log_wanted={int(r['line_number']) for r in csv.DictReader(f)}
    patterns={}; log_details=[]
    with log_path.open(encoding='utf-8',errors='replace') as f:
        for n,line in enumerate(f,1):
            if n not in log_wanted: continue
            safe=redact(line)
            normalized=re.sub(r'\b[A-Z][A-Za-z0-9_-]{10,}\b','<ID>',safe)
            normalized=re.sub(r'\d+','<N>',normalized)
            entry=patterns.setdefault(normalized,{'count':0,'first_line_number':n,'example':safe[:350]})
            entry['count']+=1
            log_details.append({'line_number':n,'text':safe})
    log_groups=sorted(patterns.values(),key=lambda x:(-x['count'],x['first_line_number']))
    report={'status':'EVIDENCE_REVIEW_NOT_FINAL_VALIDATION','validation_complete':False,
        'comparison_summary_sha256':sha(summary_path),'after_only_creation_groups':bucket_summary,
        'csv_present_before_absent_tasks':extra_csv,'root_membership_differences':membership,
        'reference_field_changes':field_changes,'log_groups':log_groups,
        'limits':['Creation within extraction window does not establish the exact folder-query timing.',
                  'Updated dates alone do not identify the cause of a difference.',
                  'Log patterns remain candidates until interpreted; no automatic failure/success inference.']}
    (out/'review_summary.json').write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
    (out/'task_evidence.json').write_text(json.dumps(details,indent=2,sort_keys=True)+'\n')
    (out/'log_candidates.json').write_text(json.dumps(log_details,indent=2,sort_keys=True)+'\n')
    print('AFTER-only tasks by creation window:')
    for r in bucket_summary: print(json.dumps(r))
    print('\nCSV task absent from BEFORE:')
    for r in extra_csv: print(json.dumps(r))
    print('\nAFTER-only tasks outside changed folders:')
    for r in details:
        if r.get('folder_group')=='OUTSIDE_CHANGED_SCOPE_FOLDERS':
            print(json.dumps({'task_id':r['task_id'],'created_dates':sorted({v['task'].get('createdDate','') for v in r['after']['variants']}),
                              'minutes':r['comparison']['after_minutes'],'creation_bucket':r['creation_bucket']}))
    print('\nRoot membership differences:')
    for r in membership: print(json.dumps(r))
    print('\nChanged fields between reference captures:')
    for r in field_changes: print(json.dumps(r))
    print('\nLog patterns (up to 12 groups; numbers are line counts, not failed requests):')
    for r in log_groups[:12]: print(json.dumps(r))
    print('Total distinct log patterns:',len(log_groups))
    print('\nSaved review:',out)
    print('No API calls. These classifications do not certify completeness.')
    return report

if __name__=='__main__':
    try: main()
    except (ValueError,OSError,KeyError,TypeError,csv.Error) as exc:
        print('Review stopped:',str(exc));raise SystemExit(2)
