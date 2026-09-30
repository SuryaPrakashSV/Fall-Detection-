cd ~/Desktop/wrike-local-baseline
python3 - <<'PY'
import csv
from pathlib import Path

files = sorted(Path("output/effort_reconciliation").glob(
    "*/gap_review_*/container_groups_saved_surya_vs_saved_akash.csv"
))
if not files:
    raise SystemExit("No container-group report found.")

with files[-1].open(encoding="utf-8-sig", newline="") as f:
    for row in csv.DictReader(f):
        if row["difference_type"] == "AKASH_ONLY":
            print("\nContainers:", row["akash_container_names_json"])
            print("Tasks:", row["task_count"])
            print("Signed hours:", row["baseline_minus_akash_hours"])
PY
