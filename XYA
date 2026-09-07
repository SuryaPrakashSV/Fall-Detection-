WITH src AS (
    SELECT *
    FROM GBI_RETAIL_BAP_DB.ASO_OPS_WSA_SNDBX_BIZ_APP.WRIKE_STG
    WHERE TRIM(CHILD_FOLDER_TITLE) ILIKE '[FR] Mac APR Update'
),
hierarchy_ids AS (
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
),
direct_task_rows AS (
    SELECT DISTINCT "id" AS task_id
    FROM src
    WHERE COALESCE(
        "parent_task_id",
        "child_task_id",
        "grandchild_task_id",
        "baby_task_id",
        "grandbaby_task_id",
        "great_grandbaby_task_id"
    ) IS NOT NULL
)
SELECT h.task_id AS hierarchy_id_without_own_row
FROM hierarchy_ids h
LEFT JOIN direct_task_rows d
    ON h.task_id = d.task_id
WHERE h.task_id IS NOT NULL
  AND d.task_id IS NULL
ORDER BY h.task_id;
