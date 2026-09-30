#!/usr/bin/env python3
"""Run the existing local CSV extractor between independent reference captures.

Run from ~/Desktop/wrike-local-baseline:
  python3 run_wrike_comparison_export.py --case output/aligned_comparison/CASE

This DOES run Wrike_Data_local_validation.py, unlike the earlier offline checks.
It requires the source hash recorded in preparation and the completed BEFORE
capture. It does not edit that source or supply/store its credentials. Enter
the same Surya token at the extractor prompt and again for the AFTER capture.

Records UTC run boundaries, console output, source/reference hashes and the
one newly created CSV. Rejects old/missing/ambiguous CSV output. Then launches
capture_wrike_reference.py --phase after. The scripts do not perform the final
comparison, which is a separate offline step. An exit code of zero from the
extractor is not proof that all its internal requests succeeded; its log must
still be reviewed. Uses the previously reviewed CSV-only extraction copy.
"""
import argparse
import ast
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    before = path.stat()
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("File changed while being fingerprinted: " + path.name)
    return result.hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def verified_before(case, source):
    preparation = json.loads((case / "manifest.json").read_text())
    if preparation.get("status") != "PREPARED_NOT_VALIDATED":
        raise ValueError("Preparation is not complete")
    frozen_path = case / "frozen_scope.json"
    if digest(frozen_path) != preparation.get("files", {}).get("frozen_scope.json"):
        raise ValueError("Frozen scope hash mismatch")
    frozen = json.loads(frozen_path.read_text())
    if digest(source) != frozen.get("source_sha256"):
        raise ValueError("Extraction source changed since preparation")
    candidates = []
    for directory in sorted(case.glob("reference_before_*")):
        path = directory / "manifest.json"
        if path.is_file():
            manifest = json.loads(path.read_text())
            if manifest.get("status") == "CAPTURE_COMPLETE_NOT_COMPARED":
                candidates.append((directory, manifest))
    if not candidates:
        raise ValueError("No completed BEFORE capture without review flags")
    directory, manifest = candidates[-1]
    if (manifest.get("phase") != "before" or manifest.get("label") != frozen.get("label") or
            manifest.get("source_sha256") != frozen.get("source_sha256") or
            manifest.get("frozen_scope_sha256") != digest(frozen_path)):
        raise ValueError("BEFORE capture does not match this scope/source")
    if not {"summary.json", "tasks.jsonl"} <= manifest.get("files", {}).keys():
        raise ValueError("BEFORE capture is missing required evidence hashes")
    for name, expected in manifest["files"].items():
        path = (directory / name).resolve()
        if directory.resolve() not in path.parents or digest(path) != expected:
            raise ValueError("BEFORE evidence failed integrity check")
    end = datetime.fromisoformat(manifest["finished_utc"])
    if end.tzinfo is None or end > datetime.now(timezone.utc):
        raise ValueError("Invalid BEFORE capture time")
    # Supplemental checks for accidental use of the original upload script.
    # These checks supplement the fixed reviewed-source hash, not a sandbox.
    text = source.read_text(encoding="utf-8-sig")
    if "LOCAL CSV EXPORT" not in text:
        raise ValueError("Local CSV export marker missing")
    tree = ast.parse(text)
    forbidden = {"write_pandas", "get_snowflake_connection", "to_sql"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if name in forbidden:
                raise ValueError("Unexpected database operation in local source; stop for review")
    return frozen, directory, manifest


def new_export(existing, directory):
    created = set(directory.glob("*/wrike_local_full.csv")) - existing
    if len(created) != 1:
        raise ValueError(f"Expected exactly one new CSV; found {len(created)}")
    path = created.pop()
    fingerprint = digest(path)
    csv.field_size_limit(16 * 1024 * 1024)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, strict=True)
        columns = reader.fieldnames or []
        if len(columns) != len(set(columns)) or not {"key", "effortAllocation_totalEffort"} <= set(columns):
            raise ValueError("New CSV does not have expected task/effort columns")
        rows = 0
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("Malformed row in new CSV")
            rows += 1
    if rows == 0 or digest(path) != fingerprint:
        raise ValueError("New CSV is empty or changed while being checked")
    return {"path": str(path.resolve()), "sha256": fingerprint,
            "bytes": path.stat().st_size, "physical_rows": rows,
            "new_file_observed": True}


def run_logged(command, cwd, log_path):
    with log_path.open("x", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, bufsize=1)
        try:
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
                log.flush()
            return process.wait()
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            raise
        finally:
            process.stdout.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=Path("Wrike_Data_local_validation.py"))
    args = parser.parse_args()
    case, source = args.case.resolve(), args.source.resolve()
    capture = Path(__file__).resolve().with_name("capture_wrike_reference.py")
    if not capture.is_file():
        raise ValueError("capture_wrike_reference.py must be beside this launcher")
    frozen, before_path, before = verified_before(case, source)
    source_hash = digest(source)
    capture_hash = digest(capture)
    output = source.parent / "output" / "local_validation"
    existing = set(output.glob("*/wrike_local_full.csv"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    run = case / ("extraction_" + stamp)
    run.mkdir(exist_ok=False)
    receipt_path = run / "receipt.json"
    receipt = {"status": "LOCAL_EXTRACTION_RUNNING", "label": frozen["label"],
               "source_path": str(source), "source_sha256": source_hash,
               "frozen_scope_sha256": digest(case / "frozen_scope.json"),
               "before_capture_path": str(before_path),
               "before_manifest_sha256": digest(before_path / "manifest.json"),
               "before_finished_utc": before["finished_utc"],
               "capture_script_sha256": capture_hash,
               "launcher_sha256": digest(Path(__file__).resolve()),
               "started_utc": now(), "validation_complete": False}
    save(receipt_path, receipt)
    print(f"Starting the existing CSV-only extractor. Use the SAME {frozen['label']} token.", flush=True)
    print("The AFTER reference capture will follow and ask for that token again.", flush=True)
    print("Run evidence:", run, flush=True)
    result = 2
    try:
        code = run_logged([sys.executable, "-u", str(source)], source.parent, run / "extractor.log")
        receipt.update({"extractor_exit_code": code, "finished_utc": now(),
                        "extractor_log_sha256": digest(run / "extractor.log")})
        if code:
            raise ValueError(f"Extractor exited with code {code}; AFTER capture not started")
        if digest(source) != source_hash:
            raise ValueError("Extraction source changed during run")
        receipt["csv"] = new_export(existing, output)
        receipt["status"] = "LOCAL_EXTRACTION_RECORDED_NOT_COMPARED"
        save(receipt_path, receipt)
        print("\nFresh CSV recorded:", receipt["csv"]["path"], flush=True)
        print("Starting AFTER capture; use the same token again.", flush=True)
        if digest(capture) != capture_hash:
            raise ValueError("Reference capture script changed during extraction")
        prior_after = set(case.glob("reference_after_*"))
        after_code = run_logged([
            sys.executable, "-u", str(capture), "--case", str(case), "--phase", "after",
            "--source", str(source)
        ], source.parent, run / "after_capture.log")
        receipt["after_capture_exit_code"] = after_code
        receipt["after_log_sha256"] = digest(run / "after_capture.log")
        after_paths = set(case.glob("reference_after_*")) - prior_after
        if len(after_paths) == 1:
            path = after_paths.pop()
            receipt["after_capture_path"] = str(path)
            if (path / "manifest.json").is_file():
                receipt["after_manifest_sha256"] = digest(path / "manifest.json")
        receipt["status"] = ("INPUTS_READY_NOT_COMPARED" if after_code == 0 and
                             "after_manifest_sha256" in receipt else "AFTER_CAPTURE_REQUIRES_REVIEW")
        result = 0 if receipt["status"] == "INPUTS_READY_NOT_COMPARED" else 3
    except BaseException as exc:
        receipt["status"] = "RUN_INCOMPLETE"
        receipt["error_type"] = type(exc).__name__
        reason = str(exc) if type(exc) is ValueError else type(exc).__name__
        receipt["reason"] = reason
        print("\nRun stopped:", reason, flush=True)
        result = 130 if isinstance(exc, KeyboardInterrupt) else 2
    finally:
        receipt["launcher_finished_utc"] = now()
        save(receipt_path, receipt)
    print("\nStatus:", receipt["status"])
    print("Receipt:", receipt_path)
    print("No final comparison has been performed. Keep all evidence files.")
    return result


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, SyntaxError) as exc:
        print("Launcher stopped before extraction:", str(exc) if type(exc) is ValueError else type(exc).__name__)
        raise SystemExit(2)
