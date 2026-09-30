#!/usr/bin/env python3
"""Generate read-only SQL for the remaining 1,154 + 196 effort exceptions.
Run beside review_wrike_effort_gaps.py. No token/API/database execution.
Summary hours remain the SAVED comparison amounts, not new live totals.
"""
import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
import review_wrike_effort_gaps as gap

AKASH_REFRESH = '2026-09-25T00:19:07+00:00'
BASELINE_REFRESH = '2026-09-29T14:44:07+00:00'


def choose(old, new):
    old_map = {r['task_id']: r for r in old}
    new_map = {r['task_id']: r for r in new}
    if len(old_map) != len(old) or len(new_map) != len(new):
        raise ValueError('Repeated task IDs in exception inputs.')
    for kind in ('BASELINE_ONLY_OUTSIDE_56', 'AKASH_ONLY'):
        for row in [r for r in old if r['difference_type'] == kind]:
            newer = new_map.get(row['task_id'])
            if not newer or newer['difference_type'] != kind:
                raise ValueError('Previously verified exception persistence has changed.')
            gap.same(newer['_delta'], row['_delta'], 'Persistent exception effort')
    selected = []
    for kind, count, hours in (('BASELINE_ONLY_OUTSIDE_56',1154,'33459.1333333333'),
                                ('AKASH_ONLY',196,'-4771')):
        subset = [r for r in new if r['difference_type'] == kind and r['task_id'] not in old_map]
        if len(subset) != count:
            raise ValueError('Expected ' + str(count) + ' additional exceptions for ' + kind)
        gap.same(gap.total(subset), hours, kind + ' saved hours')
        for row in subset:
            if not re.fullmatch(r'[A-Za-z0-9_-]+', row['task_id']):
                raise ValueError('Unexpected task ID format.')
        selected.extend(subset)
    return sorted(selected, key=lambda r:(r['difference_type'],r['task_id']))


def queries(rows):
    values = ',\n    '.join("('%s', '%s', %s)" %
        (r['task_id'],r['difference_type'],gap.exact(r['_delta'])) for r in rows)
    base = '''-- Read only. Current table observation, not historical Time Travel.
-- Group each exact metadata key once; never sum effort across repeated raw rows.
-- SAVED_SIGNED_HOURS partitions the saved gap; current values do not replace it.
-- Creation buckets compare timestamps to saved refresh markers, not exact API capture times.
-- CURRENT_KEY_ROWS > 0 proves a matching table key, not current token visibility.
-- NO_CURRENT_ROW does not prove deletion. Unknown/conflicting dates stay unclassified.
WITH requested AS (
  SELECT column1::VARCHAR AS task_id, column2::VARCHAR AS difference_type,
         column3::NUMBER(38,10) AS saved_signed_hours
  FROM VALUES
    ''' + values + '''
), matches AS (
  SELECT s."key"::VARCHAR AS matched_key, OBJECT_CONSTRUCT_KEEP_NULL(s.*) AS obj
  FROM GBI_RETAIL_BAP_DB.ASO_OPS_WSA_SNDBX_BIZ_APP."Wrike_STG" s
  JOIN requested r ON s."key"::VARCHAR = r.task_id
), parsed AS (
  SELECT matched_key,
    TRY_TO_TIMESTAMP_TZ(GET_IGNORE_CASE(obj,'createdDate')::VARCHAR) AS created_at,
    TRY_TO_TIMESTAMP_TZ(GET_IGNORE_CASE(obj,'updatedDate')::VARCHAR) AS updated_at,
    GET_IGNORE_CASE(obj,'Data refresh')::VARCHAR AS data_refresh,
    TRY_TO_DECIMAL(GET_IGNORE_CASE(obj,'effortAllocation_totalEffort')::VARCHAR,38,10) AS effort_minutes,
    GET_IGNORE_CASE(obj,'id')::VARCHAR AS container_id
  FROM matches
), per_task AS (
  SELECT r.task_id, r.difference_type, r.saved_signed_hours,
    COUNT(p.matched_key) AS current_key_rows,
    COUNT(p.created_at) AS valid_created_rows,
    COUNT(DISTINCT p.created_at) AS created_value_count,
    MIN(p.created_at) AS created_min, MAX(p.created_at) AS created_max,
    MIN(p.updated_at) AS updated_min, MAX(p.updated_at) AS updated_max,
    MIN(p.data_refresh) AS refresh_min, MAX(p.data_refresh) AS refresh_max,
    COUNT(p.effort_minutes) AS valid_effort_rows,
    COUNT(DISTINCT p.effort_minutes) AS effort_value_count,
    MIN(p.effort_minutes) AS effort_minutes_min,
    MAX(p.effort_minutes) AS effort_minutes_max,
    ARRAY_AGG(DISTINCT p.container_id) AS current_container_ids
  FROM requested r LEFT JOIN parsed p ON p.matched_key = r.task_id
  GROUP BY r.task_id, r.difference_type, r.saved_signed_hours
), classified AS (
  SELECT *,
    CASE WHEN current_key_rows = 0 THEN 'NO_CURRENT_ROW'
      WHEN valid_created_rows <> current_key_rows THEN 'MISSING_OR_INVALID_CREATED_DATE'
      WHEN created_value_count <> 1 THEN 'CONFLICTING_CREATED_DATES'
      WHEN created_min > TO_TIMESTAMP_TZ(''' + "'" + BASELINE_REFRESH + "'" + ''') THEN 'AFTER_SAVED_BASELINE_REFRESH'
      WHEN created_min > TO_TIMESTAMP_TZ(''' + "'" + AKASH_REFRESH + "'" + ''') THEN 'AFTER_AKASH_THROUGH_BASELINE_REFRESH'
      ELSE 'ON_OR_BEFORE_SAVED_AKASH_REFRESH' END AS creation_bucket,
    CASE WHEN current_key_rows = 0 THEN 'NO_CURRENT_ROW'
      WHEN valid_effort_rows <> current_key_rows OR effort_value_count <> 1 OR effort_minutes_min < 0
        THEN 'UNKNOWN_OR_CONFLICTING_CURRENT_EFFORT'
      WHEN ABS(effort_minutes_min / 60.0 - ABS(saved_signed_hours)) <= 0.000001
        THEN 'MATCHES_SAVED_REFERENCE'
      ELSE 'DIFFERS_FROM_SAVED_REFERENCE' END AS current_effort_check
  FROM per_task
)
'''
    summary = base + '''SELECT CURRENT_TIMESTAMP() AS query_checked_at,
  difference_type, creation_bucket, current_effort_check,
  COUNT(*) AS task_count, ROUND(SUM(saved_signed_hours),6) AS saved_signed_hours,
  SUM(current_key_rows) AS physical_rows,
  MIN(refresh_min) AS earliest_data_refresh, MAX(refresh_max) AS latest_data_refresh,
  MIN(created_min) AS earliest_created, MAX(created_max) AS latest_created
FROM classified
GROUP BY difference_type, creation_bucket, current_effort_check
ORDER BY difference_type, creation_bucket, current_effort_check;
'''
    detail = base + '''SELECT CURRENT_TIMESTAMP() AS query_checked_at, *
FROM classified ORDER BY difference_type, task_id;
'''
    return summary, detail


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    if args.report:
        report = args.report.expanduser().resolve()
    else:
        found = sorted((root/'output/effort_reconciliation').glob('*/effort_reconciliation_details.json'))
        if not found: raise ValueError('No effort report found.')
        report = found[-1].parent
    path = report/'effort_reconciliation_details.json'
    digest = gap.sha(path)
    details = json.loads(path.read_text(encoding='utf-8-sig'))
    # Refuse to reuse fixed reference markers with a different source snapshot.
    for name, expected in (('akash','2026-09-24 19:19:07 CDT-0500'),
                           ('snowflake','2026-09-29 09:44:07 CDT-0500')):
        markers = details['datasets'][name]['input']['data_refresh_counts']
        if set(markers) != {expected}:
            raise ValueError(name + ' refresh differs from this investigation. Do not use these cutoffs.')
    data, audits = {}, {}
    for label in (gap.OLD,gap.NEW):
        comparison = details['comparisons'][label]
        name = comparison['task_difference_file']
        if Path(name).name != name: raise ValueError('Unexpected difference filename.')
        data[label], audits[label] = gap.read_differences(report/name,comparison)
    selected = choose(data[gap.OLD],data[gap.NEW])
    summary, detail = queries(selected)
    if gap.sha(path) != digest: raise ValueError('Report changed during preparation.')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    out=root/'output/remaining_effort_review'/stamp
    out.mkdir(parents=True,exist_ok=False)
    (out/'01_remaining_effort_summary.sql').write_text(summary,encoding='utf-8')
    (out/'02_remaining_effort_details.sql').write_text(detail,encoding='utf-8')
    (out/'query_inputs.json').write_text(json.dumps(dict(details_sha256=digest,
        difference_inputs=audits, selected_count=len(selected),
        akash_refresh_utc=AKASH_REFRESH, baseline_refresh_utc=BASELINE_REFRESH),indent=2)+'\n')
    print('Verified: 1,154 additional baseline-only tasks; +33,459.13 saved hours.')
    print('Verified: 196 additional Akash-only tasks; -4,771.00 saved hours.')
    print('Run this SQL in Snowflake FIRST:',out/'01_remaining_effort_summary.sql')
    print('The result is a small grouped summary. Share every summary row and column.')
    print('Detailed SQL is saved beside it if follow-up is needed. Do not run it yet.')
    print('No API calls, full extraction, or database execution performed by this helper.')


if __name__ == '__main__':
    try: main()
    except (ValueError, KeyError, OSError) as exc: raise SystemExit('STOPPED: '+str(exc))
