#!/usr/bin/env python3
"""Rebuild the verified Wrike workbook with a business guide and closeout evidence.
Offline only; original workbook, CSV, scripts and API captures remain unchanged.
"""
import argparse
import csv
import hashlib
import importlib.util
import json
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

# Filled with the reviewed collector/validator digest before delivery.
EXTRA_CAPTURE_VALIDATOR_SHA256 = '4eb7d5c67d3796f509ceec86b4120a2227bed4ea1bba14f45825974db221fc99'


def check(ok,message):
    if not ok:raise ValueError(message)


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1048576),b''):h.update(b)
    return h.hexdigest()


def load(path):
    with Path(path).open(encoding='utf-8-sig') as f:return json.load(f)


def csv_rows(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))


def verify_manifest(directory,name='output_hashes.json'):
    hashes=load(directory/name)
    for rel,digest in hashes.items():
        p=Path(rel);target=(directory/p).resolve()
        check(not p.is_absolute() and '..' not in p.parts and target.is_relative_to(directory.resolve()),'Unsafe evidence path')
        check(target.is_file() and sha(target)==digest,'Evidence hash mismatch: '+str(rel))
    return hashes


def import_verified(name,path,digest):
    check(path.is_file() and sha(path)==digest,'Reviewed script differs from original workbook receipt: '+str(path))
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module;spec.loader.exec_module(module)
    return module


def number_cells(rows,fields):
    for row in rows:
        for key in fields:
            if row.get(key,'')!='':row[key]=Decimal(str(row[key]))
    return rows


def original_inputs(root,snapshot,workbook_dir=None,closeout_dir=None):
    digest=sha(snapshot)
    candidates=[Path(workbook_dir).resolve()/'workbook_receipt.json'] if workbook_dir else [
        p for p in (root/'output'/'holistic_workbook').glob('*/workbook_receipt.json')
        if load(p).get('status')=='WORKBOOK_CREATED_REVIEW_REQUIRED'
        and load(p).get('source',{}).get('snapshot_sha256')==digest]
    check(len(candidates)==1,'Supply --workbook-dir: expected one original workbook matching this snapshot')
    receipt_path=candidates[0];receipt=load(receipt_path);directory=receipt_path.parent
    check(receipt.get('status')=='WORKBOOK_CREATED_REVIEW_REQUIRED','Original workbook is incomplete')
    check(receipt['source']['snapshot_sha256']==digest,'Snapshot differs from original workbook')
    workbook=directory/Path(receipt['workbook']).name
    check(sha(workbook)==receipt['workbook_sha256'],'Original workbook changed since its receipt')
    verify_manifest(directory/'supporting_calculations')
    closes=[Path(closeout_dir).resolve()/'summary.json'] if closeout_dir else [
        p for p in (directory/'closeout').glob('*/summary.json')
        if load(p).get('status')=='OFFLINE_CLOSEOUT_CHECK_COMPLETE_REVIEW_REQUIRED'
        and load(p).get('workbook_sha256')==receipt['workbook_sha256']]
    check(len(closes)==1,'Supply --closeout-dir: expected one matching completed closeout check')
    close_path=closes[0];close=load(close_path);covered=verify_manifest(close_path.parent)
    check({'summary.json','neither_scope_groups.csv','missing_project_changes.csv','api_only_projects.csv'}<=set(covered),
          'Closeout output hashes do not cover all required inputs')
    check(close['snapshot_sha256']==digest and close['workbook_sha256']==receipt['workbook_sha256']
          and close['receipt_sha256']==sha(receipt_path),'Closeout does not bind this snapshot/workbook/receipt')
    check(not close.get('scope_flags'),'Resolve the closeout scope flags before finalizing')
    history=close.get('missing_project_comparison',{})
    if history.get('status')=='COMPARED':
        check(sha(Path(history['path']))==history['sha256'],'Historical missing-project report changed since closeout')
    return receipt_path,receipt,close_path,close


def run(root,snapshot,workbook_dir=None,closeout_dir=None,builder_path=None,extra_capture=None):
    root=Path(root).resolve();snapshot=Path(snapshot).resolve()
    receipt_path,receipt,close_path,close=original_inputs(root,snapshot,workbook_dir,closeout_dir)
    candidates=[Path(builder_path).resolve()] if builder_path else [
        p for p in (root/'WBH.py',root/'build_wrike_workbook.py')
        if p.is_file() and sha(p)==receipt['builder_sha256']]
    check(candidates,'Cannot find the reviewed builder. Keep WBH.py unchanged or supply --builder.')
    analysis=import_verified('wrike_report_analysis',root/'wrike_report_analysis.py',receipt['source']['analysis_script_sha256'])
    builder=import_verified('_verified_wrike_builder',candidates[0],receipt['builder_sha256'])
    print('Verifying the original snapshot and saved access evidence...',flush=True)
    report=analysis.analyze(snapshot,receipt['source']['case_path'])
    check(json.loads(json.dumps(report['summary'],default=str))==receipt['summary'],'Recalculated summary differs from original workbook')
    recon={r['metric_id']:r for r in report['reconciliation_rows']}
    check(Decimal(str(close['neither_effort_minutes']))==recon['NEITHER']['effort_minutes'],'Closeout outside-project effort differs')
    folder_rows=number_cells(csv_rows(close_path.parent/'neither_scope_groups.csv'),['task_count','effort_minutes','effort_hours'])
    check(sum((r['effort_minutes'] for r in folder_rows),Decimal(0))==recon['NEITHER']['effort_minutes']
          and sum((r['task_count'] for r in folder_rows),Decimal(0))==recon['NEITHER']['task_count'],
          'Folder-scope details differ from closeout totals')
    change_rows=number_cells(csv_rows(close_path.parent/'missing_project_changes.csv'),['historical_effort_hours','current_effort_hours','effort_hours_change'])
    expected_extra={p['project_id'] for p in report['project_rows'] if not p['in_frozen_snapshot']}
    extra_rows=[{'project_id':p['project_id'],'project_name':p['project_name'],
                 'snapshot_effort_hours':None,'current_api_effort_hours':None,
                 'status':'NOT_IN_FROZEN_SNAPSHOT','note':'Snapshot effort is unknown; excluded from frozen-snapshot totals.'}
                for p in report['project_rows'] if not p['in_frozen_snapshot']]
    extra_meta={'status':'NOT_PROVIDED','api_only_project_count':len(expected_extra)}
    extra_tasks=[];extra_links=[];extra_issues=[]
    extra_paths=[Path(extra_capture).resolve()] if extra_capture else [
        p.parent for p in (root/'output'/'extra_project_effort').glob('*/summary.json')
        if load(p).get('status')=='EXTRA_PROJECT_CAPTURE_COMPLETE_REVIEW_REQUIRED'
        and load(p).get('source',{}).get('workbook_sha256')==receipt['workbook_sha256']
        and load(p).get('source',{}).get('snapshot_sha256')==report['metadata']['snapshot_sha256']
        and set(load(p).get('source',{}).get('project_ids',[]))==expected_extra]
    check(len(extra_paths)<=1,'Several matching extra-project captures; supply --extra-capture')
    check(extra_paths or not expected_extra,
          'Run capture_wrike_extra_projects.py first: no completed matching API-only project capture was found')
    if extra_paths:
        check(EXTRA_CAPTURE_VALIDATOR_SHA256,'Extra-capture validator was not pinned in this delivered finalizer')
        collector=import_verified('_verified_extra_project_capture',root/'capture_wrike_extra_projects.py',EXTRA_CAPTURE_VALIDATOR_SHA256)
        extra_summary,live_projects,extra_tasks,extra_links,extra_issues=collector.validate_saved_capture(extra_paths[0])
        source=extra_summary['source']
        check(source['snapshot_sha256']==report['metadata']['snapshot_sha256']
              and source['workbook_sha256']==receipt['workbook_sha256']
              and source['workbook_receipt_sha256']==sha(receipt_path)
              and source['access_manifest_sha256']==report['metadata']['capture_hash_manifest_sha256'],
              'Extra capture is not bound to this frozen snapshot/workbook/access evidence')
        check(set(source['project_ids'])==expected_extra and {p['project_id'] for p in live_projects}==expected_extra,
              'Extra capture project IDs do not match the API-only projects')
        check(source['capture_script_sha256']==EXTRA_CAPTURE_VALIDATOR_SHA256,'Extra capture used a different collector version')
        extra_rows=[]
        for p in live_projects:
            extra_rows.append({'project_id':p['project_id'],'project_name':p['project_name'],
                 'snapshot_effort_hours':None,'current_api_effort_hours':p['effort_hours'],
                 'known_current_subtotal_hours':p['known_subtotal_hours'],
                 'task_count':p['task_count'],'unknown_effort_task_count':p['unknown_effort_task_count'],
                 'mode_none_without_total_count':p['mode_none_without_total_count'],'other_unknown_count':p['other_unknown_count'],
                 'unreturned_subtask_reference_count':p['unreturned_subtask_reference_count'],
                 'tasks_already_in_snapshot':p['tasks_already_in_snapshot'],'tasks_absent_from_snapshot':p['tasks_absent_from_snapshot'],
                 'known_hours_already_in_snapshot':p['known_hours_already_in_snapshot'],
                 'known_hours_absent_from_snapshot':p['known_hours_absent_from_snapshot'],
                 'status':p['effort_status'],'capture_started_utc':extra_summary['started_at'],
                 'capture_finished_utc':extra_summary['finished_at'],
                 'note':'Later live API effort; never added to frozen Snowflake reconciliation.'})
        extra_meta={'status':'VALIDATED_SAVED_CAPTURE','path':str(extra_paths[0]),
                    'hash_manifest_sha256':sha(extra_paths[0]/'hashes.json'),'summary':extra_summary}
    output=root/'output'/'holistic_final'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    output.mkdir(parents=True,exist_ok=False)
    check(shutil.disk_usage(output).free>=max(2*1024**3,snapshot.stat().st_size*12),'Insufficient disk space for workbook build')
    analysis.write_outputs(report,output/'supporting_calculations')
    base_writer=builder.Writer

    class FinalWriter(base_writer):
        def __init__(self,path):
            super().__init__(path)
            self.new_sheet('Report Guide')

        def write_overflow(self):
            self.generic('Folder Scope',folder_rows,'Disjoint groups of tasks with no confirmed project association in the exported hierarchy.')
            self.generic('Missing List Changes',change_rows,'Dated missing-project lists; changes do not prove permission changes. Hours compared at 10 decimal places.')
            self.generic('Extra Project Effort',extra_rows,'Projects visible to Akash but absent from the frozen Snowflake snapshot. Separate from snapshot totals.')
            if extra_meta['status']=='VALIDATED_SAVED_CAPTURE':
                self.generic('Extra Tasks',extra_tasks,'Later API task evidence, including unmodified effort allocation JSON and source response filenames.')
                self.generic('Extra Project Links',extra_links,'One project/task association per row from the later scoped API responses.')
                self.generic('Extra Review Issues',extra_issues,'Unreturned subtask references or other issues from the later capture.')
            s=self.sheets['Report Guide']
            self.heading(s,'Wrike project access and planned effort','Read this guide first, then use the detail tabs to trace every total.',
                         ['Question','Answer','Where to verify'],[52,95,36])
            meta=report['metadata'];summary=report['summary'];comparison=close['missing_project_comparison']
            rows=[('What this report measures','Planned task effort in one frozen Snowflake snapshot, grouped by project retrieval observed with Akash\'s token.','Reconciliation'),
                  ('Backend effort field','Wrike effortAllocation.totalEffort in minutes; Snowflake effortAllocation_totalEffort. Hours = minutes / 60 per unique own task ID, all statuses.','Task Effort / Snowflake Snapshot'),
                  ('Snapshot exported','; '.join(meta['snapshot_timestamps']),'Snowflake Snapshot'),
                  ('Source refresh, as recorded','; '.join(meta['data_refresh_values']),'Snowflake Snapshot'),
                  ('Project access checked, UTC',meta['capture_started_at']+' to '+meta['capture_finished_at'],'Access Evidence'),
                  ('Complete snapshot',f"{summary['snapshot_data_rows']:,} data rows; {summary['snapshot_column_count']} columns; {summary['unique_task_count']:,} unique eligible tasks.",'Snowflake Snapshot / Task Effort'),
                  ('Total unique task effort (hours)',recon['SNAPSHOT_UNIQUE_TASKS']['effort_hours'],'Reconciliation'),
                  ('Tasks under accessible projects (hours)',recon['ACCESSIBLE_PROJECT_UNION']['effort_hours'],'Accessible Projects'),
                  ('Tasks under unavailable projects (hours)',recon['MISSING_PROJECT_UNION']['effort_hours'],'Missing Projects'),
                  ('Shared between both project groups (hours)',recon['BOTH']['effort_hours'],'Task Effort: BOTH'),
                  ('Tasks outside both project groups (hours)',recon['NEITHER']['effort_hours'],'Folder Scope'),
                  ('How the total reconciles','Accessible + unavailable − shared overlap + outside-project scope = frozen snapshot total.','Reconciliation'),
                  ('Displayed rounding','Displayed hours are rounded; reconciliation uses unrounded minutes. Adding displayed rounded figures may differ slightly.','Task Effort / Reconciliation'),
                  ('Why project rows do not simply add up','A task can belong to several projects. Project row sums repeat shared tasks; the reconciliation counts each task once.','Project Task Links'),
                  ('Outside-project scope means','These tasks have no confirmed project association in the stored hierarchy. This is not evidence that their task data is inaccessible.','Folder Scope'),
                  ('Current missing-project list',f"{summary['baseline_missing_project_count']} baseline projects returned unavailable. 403/404 does not establish the historical cause.",'Missing Projects'),
                  ('Visible projects absent from snapshot',f"{summary['api_only_project_count']} projects. Frozen snapshot effort is unknown, not zero; any later API effort is shown separately.",'Extra Project Effort'),
                  ('What is verified','Saved input hashes, source row identities, duplicate task effort, project membership accounting and raw project-access evidence.','Task Effort / Access Evidence'),
                  ('What is not established','Project retrieval does not establish access to every task. A balanced partition is not independent proof of 100% extraction completeness.','Reconciliation'),
                  ('Earlier historical limitations','Earlier deletion dates and two older zero-effort omissions remain unverified. They are separate from this frozen-snapshot accounting.','Earlier historical reconciliation'),
                  ('How to use this workbook','Start with this guide, review Missing Projects, then trace effort through Project Task Links and Task Effort to Snowflake Snapshot.','All supporting data is in this workbook')]
            if extra_meta['status']=='VALIDATED_SAVED_CAPTURE':
                live=extra_meta['summary']
                rows.extend([
                    ('Five-project supplement captured, UTC',live['started_at']+' to '+live['finished_at'],'Extra Project Effort'),
                    ('Supplement numeric effort subtotal (hours)',live['known_union_effort_hours'],'Extra Tasks'),
                    ('Supplement effort without explicit total',f"{live['mode_none_without_total_count']} tasks have effort mode None without a total; {live['other_unknown_count']} have other unknown effort; {live['unreturned_subtask_reference_count']} subtask references were not returned; {live.get('task_scope_issue_count',0)} task scope issues.",'Extra Tasks / Extra Review Issues'),
                    ('Supplement scope',f"{live['unique_task_count']} unique tasks; {live['tasks_already_in_snapshot']} task IDs also occur in the frozen snapshot. Do not add these live values to the frozen total.",'Extra Tasks / Extra Project Links')])
            if comparison.get('status')=='COMPARED':
                rows.insert(14,('Change from the earlier missing list',f"Added {comparison['added_count']}; removed {comparison['removed_count']}; shared {comparison['shared_count']}; shared projects with changed effort {comparison.get('shared_changed_count','unknown')}.",'Missing List Changes'))
                added=[r for r in change_rows if r['change']=='ADDED']
                for item in added[:5]:rows.insert(15,('New entry: '+item['name'],item['current_effort_hours'],'Missing List Changes'))
            else:rows.insert(14,('Earlier missing-list comparison','Historical report was not available for exact comparison.','Missing List Changes'))
            wrapped=self.book.add_format({'font_name':'Arial','font_size':10,'text_wrap':True,'valign':'top'})
            guide_number=self.book.add_format({'font_name':'Arial','font_size':10,'num_format':'#,##0.00;[Red](#,##0.00)',
                                              'align':'left','valign':'top'})
            for n,items in enumerate(rows,builder.START):
                s.set_row(n,36)
                for c,value in enumerate(items):self.value(s,n,c,value,guide_number if isinstance(value,Decimal) else wrapped)
            self.finish_table(s,len(rows),3);s.activate();s.set_tab_color('#17365D')
            s.set_landscape();s.fit_to_pages(1,0)
            super().write_overflow()

    builder.Writer=FinalWriter
    temporary=output/'Wrike_Project_Access_and_Effort_FINAL_BUILDING.xlsx'
    target=output/'Wrike_Project_Access_and_Effort_2026-09-30_FINAL.xlsx'
    ranges,long_count=builder.write_report(report,snapshot,temporary)
    with zipfile.ZipFile(temporary) as z:check(z.testzip() is None,'Workbook ZIP verification failed')
    check(sha(snapshot)==receipt['source']['snapshot_sha256'],'Snapshot changed during workbook finalization')
    temporary.rename(target)
    final={'status':'FINAL_WORKBOOK_CREATED_REVIEW_REQUIRED','workbook':str(target),'workbook_sha256':sha(target),
           'source_receipt':str(receipt_path),'source_receipt_sha256':sha(receipt_path),
           'closeout_summary':str(close_path),'closeout_summary_sha256':sha(close_path),
           'finalizer_sha256':sha(Path(__file__)),'original_builder_sha256':receipt['builder_sha256'],
           'summary':report['summary'],'sheet_counts':ranges,'extra_project_capture':extra_meta,
           'oversized_cells_preserved_in_chunks':long_count}
    with (output/'final_workbook_receipt.json').open('x',encoding='utf-8') as f:json.dump(final,f,indent=2,default=str)
    print('\nFINAL WORKBOOK CREATED — review before sharing')
    print('EXCEL FILE:',target);print('RECEIPT:',output/'final_workbook_receipt.json')
    return final


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--snapshot',default='snowflake_snapshot.csv');p.add_argument('--workbook-dir')
    p.add_argument('--closeout-dir');p.add_argument('--builder');p.add_argument('--extra-capture')
    a=p.parse_args();run(Path.cwd(),a.snapshot,a.workbook_dir,a.closeout_dir,a.builder,a.extra_capture)


if __name__=='__main__':
    try:main()
    except (ValueError,OSError,KeyError,csv.Error,ImportError) as e:raise SystemExit('FINALIZATION STOPPED: '+str(e))
