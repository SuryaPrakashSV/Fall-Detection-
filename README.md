WITH src AS (
    SELECT *
    FROM GBI_RETAIL_BAP_DB.ASO_OPS_WSA_SNDBX_BIZ_APP.WRIKE_STG
    WHERE TRIM(CHILD_FOLDER_TITLE) ILIKE '[FR] Mac APR Update'
),
all_task_ids AS (
    SELECT "parent_task_id" AS task_id FROM src
    UNION
    SELECT "child_task_id" FROM src
    UNION
    SELECT "grandchild_task_id" FROM src
    UNION
    SELECT "baby_task_id" FROM src
    UNION
    SELECT "grandbaby_task_id" FROM src
    UNION
    SELECT "great_grandbaby_task_id" FROM src
)
SELECT 'RAW_ROWS' AS metric, COUNT(*) AS result FROM src

UNION ALL

SELECT 'PROJECT_LEVEL_ROWS', COUNT(*)
FROM src
WHERE COALESCE(
    "parent_task_id",
    "child_task_id",
    "grandchild_task_id",
    "baby_task_id",
    "grandbaby_task_id",
    "great_grandbaby_task_id"
) IS NULL

UNION ALL

SELECT 'TOP_LEVEL_SECTION_IDS', COUNT(DISTINCT "parent_task_id")
FROM src

UNION ALL

SELECT 'CHILD_TASK_IDS', COUNT(DISTINCT "child_task_id")
FROM src

UNION ALL

SELECT 'GRANDCHILD_TASK_IDS', COUNT(DISTINCT "grandchild_task_id")
FROM src

UNION ALL

SELECT 'BABY_TASK_IDS', COUNT(DISTINCT "baby_task_id")
FROM src

UNION ALL

SELECT 'GRANDBABY_TASK_IDS', COUNT(DISTINCT "grandbaby_task_id")
FROM src

UNION ALL

SELECT 'GREAT_GRANDBABY_TASK_IDS',
       COUNT(DISTINCT "great_grandbaby_task_id")
FROM src

UNION ALL

SELECT 'TOTAL_DISTINCT_TASK_SECTION_IDS', COUNT(DISTINCT task_id)
FROM all_task_ids
WHERE task_id IS NOT NULL;
