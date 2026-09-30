#!/usr/bin/env python3
"""Build a read-only Snowflake query for the 31 saved changed-effort tasks.
Reads the validated difference report; no token, API call or database execution.
"""
import argparse
import json
import re
from pathlib import Path
from datetime import datetime, timezone
import review_wrike_effort_gaps as gap


def make_query(rows):
    selected = [r for r in rows if r['difference_type'] == 'SHARED_EFFORT_CHANGED']
    if len(selected) != 31 or len({r['task_id'] for r in selected}) != 31:
        raise ValueError('Expected exactly 31 distinct changed-effort task IDs.')
    gap.same(gap.total(selected), '3473.5333333333', 'Changed effort total')
    for row in selected:
        if not re.fullmatch(r'[A-Za-z0-9_-]+', row['task_id']):
            raise ValueError('Unexpected task ID format; SQL not generated.')
    ids = ',\n        '.join("('" + r['task_id'] + "')" for r in selected)
    # Preserve raw rows: no sum, deduplication, or assumed timestamp columns.
    # OBJECT values expose available timestamps without failing on missing fields.
    return '''-- Read only. Current Wrike_STG rows, not a historical September 29 snapshot.
-- Exact metadata key match; do not match ancestors through task hierarchy IDs.
-- Multiple physical rows per task are expected. Do not SUM their effort values.
-- Empty timestamps can indicate absent fields or null source values.
WITH requested AS (
    SELECT column1::VARCHAR AS task_id FROM VALUES
        ''' + ids + '''
), raw_rows AS (
    SELECT s."key"::VARCHAR AS task_key,
           OBJECT_CONSTRUCT_KEEP_NULL(s.*) AS payload
    FROM GBI_RETAIL_BAP_DB.ASO_OPS_WSA_SNDBX_BIZ_APP."Wrike_STG" s
    JOIN requested r ON s."key"::VARCHAR = r.task_id
)
SELECT CURRENT_TIMESTAMP() AS query_checked_at,
       r.task_id AS requested_task_id,
       CASE WHEN x.task_key IS NULL THEN 'NO_CURRENT_ROW' ELSE 'CURRENT_ROW' END AS row_presence,
       GET_IGNORE_CASE(x.payload, 'title')::VARCHAR AS task_title,
       GET_IGNORE_CASE(x.payload, 'createdDate')::VARCHAR AS created_date,
       GET_IGNORE_CASE(x.payload, 'updatedDate')::VARCHAR AS updated_date,
       GET_IGNORE_CASE(x.payload, 'Data refresh')::VARCHAR AS data_refresh,
       GET_IGNORE_CASE(x.payload, 'effortAllocation_totalEffort')::VARCHAR AS raw_effort_minutes,
       GET_IGNORE_CASE(x.payload, 'id')::VARCHAR AS container_id,
       x.payload AS raw_row
FROM requested r
LEFT JOIN raw_rows x ON x.task_key = r.task_id
ORDER BY requested_task_id;
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    if args.report:
        report = args.report.expanduser().resolve()
    else:
        found = sorted((root / 'output/effort_reconciliation').glob('*/effort_reconciliation_details.json'))
        if not found:
            raise ValueError('No effort report found.')
        report = found[-1].parent
    source = report / 'effort_reconciliation_details.json'
    digest = gap.sha(source)
    details = json.loads(source.read_text(encoding='utf-8-sig'))
    comparison = details['comparisons'][gap.NEW]
    name = comparison['task_difference_file']
    if Path(name).name != name:
        raise ValueError('Unexpected difference filename.')
    rows, audit = gap.read_differences(report / name, comparison)
    query = make_query(rows)
    if gap.sha(source) != digest:
        raise ValueError('Report changed while reading.')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    out = root / 'output/changed_effort_review' / stamp
    out.mkdir(parents=True, exist_ok=False)
    sql = out / 'inspect_31_effort_changes.sql'
    sql.write_text(query, encoding='utf-8')
    (out / 'query_inputs.json').write_text(json.dumps(dict(details_sha256=digest,
        difference_input=audit, selected_tasks=31, selected_net_hours='3473.5333333333'), indent=2)+'\n')
    print('Verified 31 task IDs; net effort change +3,473.53 hours.')
    print('SQL saved:', sql)
    print('Open this SQL file, run it in Snowflake, and export as changed_effort_task_evidence.csv.')
    print('No API calls or database queries were executed by this helper.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError) as exc:
        raise SystemExit('STOPPED: ' + str(exc))
