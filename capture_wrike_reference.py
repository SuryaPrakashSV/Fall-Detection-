#!/usr/bin/env python3
"""Capture an independent task/effort reference for a prepared comparison.

Run in ~/Desktop/wrike-local-baseline, with prepare_wrike_comparison.py present:
  python3 capture_wrike_reference.py --case output/aligned_comparison/CASE --phase before

Only GET requests to Wrike. No extraction-source execution, Snowflake connection,
or external writes. New local evidence directory only. A capture is not a
comparison or a validation result. Before and after captures must bracket a NEW
CSV-only extraction with the same token and scope; ordering is checked later.

Uses paginated folders/{root}/tasks with descendants=true and subTasks=true.
Reference discovery never reads task IDs from the extraction under test.
All statuses are requested by omitting status filters, matching the extractor.
Raw API pages, request times, hashes, membership and conflicting observations
are retained. Missing effort is never silently converted to a numeric zero.

Requires the reviewed prepare_wrike_comparison.py delivered with this workflow.
The helper is loaded only after checking its bytes or normalized Python AST.
Line endings, comments and formatting may differ; Python code must match. The
production/local extraction source is parsed as text and is NEVER executed.

Official request parameters:
https://developers.wrike.com/reference/getfolderssingletasks
https://developers.wrike.com/reference/getspacessinglefolders
"""
import argparse
import ast
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import getpass
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import types

HELPER_SHA256 = "abaa98653440d7aead3730649cd2c3aa4317b0bad3290c92f3ce0ce90afcf93b"
HELPER_CODE_SHA256 = "9a6b07d94dda227c9b34e03ac0b5fa26826831fde12b11c2ab4ac92edc2bed77"
FIELDS = ["effortAllocation", "parentIds", "superParentIds", "superTaskIds", "subTaskIds"]
RELATIONS = FIELDS[1:]
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def code_signature(tree):
    def canonical(node):
        if isinstance(node, ast.AST):
            # Ignore empty optional fields added by newer Python versions.
            return {"node": type(node).__name__, "fields": {
                key: canonical(value) for key, value in ast.iter_fields(node)
                if value is not None and value != []}}
        if isinstance(node, list):
            return [canonical(value) for value in node]
        return node
    return sha(json.dumps(canonical(tree), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True).encode())


def verified_helper_tree(raw):
    tree = ast.parse(raw)
    actual_code = code_signature(tree)
    if sha(raw) != HELPER_SHA256 and actual_code != HELPER_CODE_SHA256:
        raise ValueError("Preparation helper Python code differs from reviewed version; "
                         "no API calls made. Helper bytes SHA256=" + sha(raw) +
                         "; helper code SHA256=" + actual_code)
    return tree


def load_helper():
    path = Path(__file__).resolve().with_name("prepare_wrike_comparison.py")
    tree = verified_helper_tree(path.read_bytes())
    module = types.ModuleType("wrike_comparison_scope_helper")
    module.__file__ = str(path)
    # Execute exactly the verified helper AST, avoiding stale bytecode or a reread.
    # This is our preparation helper, never the user's extraction source.
    exec(compile(tree, str(path), "exec"), module.__dict__)
    return module


def read_case(case, helper):
    manifest = json.loads((case / "manifest.json").read_text())
    if manifest.get("status") != "PREPARED_NOT_VALIDATED":
        raise ValueError("Case is not a successful preparation")
    for name in ("frozen_scope.json", "folder_tree.json", "comparison_specification.json"):
        raw = (case / name).read_bytes()
        if sha(raw) != manifest.get("files", {}).get(name):
            raise ValueError("Prepared evidence hash mismatch: " + name)
    scope = json.loads((case / "frozen_scope.json").read_text())
    if not scope.get("root_ids") or not isinstance(scope.get("folder_ids"), list):
        raise ValueError("Missing frozen scope")
    for identity in [scope.get("space_id"), *scope["root_ids"], *scope["folder_ids"]]:
        if not isinstance(identity, str) or not ID_PATTERN.fullmatch(identity):
            raise ValueError("Invalid identity in frozen scope")
    if (scope.get("label") not in ("surya", "akash") or
            scope.get("source_sha256") != manifest.get("source_sha256") or
            scope.get("label") != manifest.get("label") or
            scope.get("space_id") != manifest.get("space_id")):
        raise ValueError("Inconsistent preparation metadata")
    tree = json.loads((case / "folder_tree.json").read_text())
    _, rebuilt = helper.prepare_scope(tree["data"])
    for field in ("root_ids", "folder_ids", "folder_structure_sha256"):
        if scope.get(field) != rebuilt.get(field):
            raise ValueError("Frozen scope does not match its saved folder inventory")
    return scope, manifest


def effort_class(task):
    """Return a category and an explicit numeric total, or None (never impute)."""
    if "effortAllocation" not in task:
        return "MISSING_EFFORT_FIELD", None
    allocation = task["effortAllocation"]
    if not isinstance(allocation, dict):
        return "NULL_OR_INVALID_ALLOCATION", None
    if "totalEffort" not in allocation:
        if allocation.get("mode") == "None" and allocation.get("responsibleAllocation") == []:
            return "MODE_NONE_WITHOUT_TOTAL", None
        return "MISSING_TOTAL_REQUIRES_REVIEW", None
    value = allocation["totalEffort"]
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return "INVALID_TOTAL", None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return "INVALID_TOTAL", None
    if not number.is_finite() or number < 0 or (number and abs(number.adjusted()) > 30):
        return "INVALID_TOTAL", None
    if allocation.get("mode") == "None" and number != 0:
        return "MODE_NONE_WITH_POSITIVE_TOTAL_REQUIRES_REVIEW", None
    return ("EXPLICIT_ZERO" if number == 0 else "EXPLICIT_POSITIVE"), str(number)


def task_projection(task):
    if not isinstance(task, dict) or not isinstance(task.get("id"), str) or not ID_PATTERN.fullmatch(task["id"]):
        raise ValueError("Malformed task ID")
    # Preserve the difference between absent and explicit null values.
    names = ["id", "scope", "status", "createdDate", "updatedDate", "effortAllocation", *RELATIONS]
    result = {name: task[name] for name in names if name in task}
    for name in RELATIONS:
        if name in result:
            value = result[name]
            if not isinstance(value, list) or any(not isinstance(x, str) or not ID_PATTERN.fullmatch(x) for x in value):
                raise ValueError("Malformed task relationship field: " + name)
            result[name] = sorted(value)
    return result


class TaskIndex:
    def __init__(self):
        self.tasks = {}
        self.referenced_children = set()
        self.in_root_repeats = Counter()

    def add(self, task, root, response_file, observed_utc):
        value = task_projection(task)
        tid = value["id"]
        item = self.tasks.setdefault(tid, {"root_ids": set(), "variants": {}})
        if root in item["root_ids"]:
            self.in_root_repeats[root] += 1
        item["root_ids"].add(root)
        digest = sha(encoded(value))
        variant = item["variants"].setdefault(digest, {"task": value, "observations": []})
        variant["observations"].append({"root_id": root, "response_file": response_file,
                                        "observed_utc": observed_utc})
        self.referenced_children.update(value.get("subTaskIds", []))

    def summarize(self):
        categories = Counter()
        explicit_minutes = Decimal(0)
        conflicts, missing_relations, missing_dates, non_workspace = [], [], [], []
        for tid, item in self.tasks.items():
            if len(item["variants"]) != 1:
                conflicts.append(tid)
                categories["CONFLICTING_OBSERVATIONS"] += 1
                continue
            task = next(iter(item["variants"].values()))["task"]
            category, numeric = effort_class(task)
            categories[category] += 1
            if numeric is not None:
                explicit_minutes += Decimal(numeric)
            if any(k not in task for k in RELATIONS):
                missing_relations.append(tid)
            if not task.get("createdDate") or not task.get("updatedDate"):
                missing_dates.append(tid)
            if task.get("scope") != "WsTask":
                non_workspace.append(tid)
        missing_children = sorted(self.referenced_children - self.tasks.keys())
        return {
            "unique_tasks": len(self.tasks), "effort_categories": dict(categories),
            "explicit_numeric_minutes": str(explicit_minutes),
            "explicit_numeric_hours": str(explicit_minutes / Decimal(60)),
            "numeric_sum_is_not_a_validated_complete_total": True,
            "conflicting_task_ids": sorted(conflicts),
            "missing_relationship_task_ids": sorted(missing_relations),
            "missing_timestamp_task_ids": sorted(missing_dates),
            "unexpected_task_scope_ids": sorted(non_workspace),
            "referenced_subtask_ids_not_returned": missing_children,
            "repeated_task_observations_within_root": dict(self.in_root_repeats),
        }

    def save(self, path):
        with path.open("w", encoding="utf-8") as handle:
            for tid, item in sorted(self.tasks.items()):
                value = {"task_id": tid, "root_ids": sorted(item["root_ids"]),
                         "variants": list(item["variants"].values())}
                handle.write(json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n")


class Capture:
    def __init__(self, out, session, token, metadata):
        self.out, self.session, self.token = out, session, token
        self.manifest = {**metadata, "status": "RUNNING", "started_utc": now(),
                         "requests": [], "files": {}, "completed_roots": []}

    def save(self, name, value):
        raw = encoded(value)
        (self.out / name).write_bytes(raw)
        if name != "manifest.json":
            self.manifest["files"][name] = sha(raw)

    def checkpoint(self):
        self.save("manifest.json", self.manifest)

    def fetch(self, endpoint, params=None):
        # At most three attempts, each individually recorded. A recovered error
        # stays visible; exhausted retries stop the capture, never skip a page.
        for attempt in range(1, 4):
            number = len(self.manifest["requests"]) + 1
            filename = f"responses/{number:06d}.json"
            item = {"endpoint": endpoint, "params": params, "attempt": attempt,
                    "started_utc": now(), "response_file": filename}
            self.manifest["requests"].append(item)
            retry_delay = None
            try:
                response = self.session.get("https://www.wrike.com/api/v4/" + endpoint,
                                            params=params, timeout=(15, 60), allow_redirects=False)
                item.update({"status": response.status_code,
                             "server_date": response.headers.get("Date")})
                raw = response.content
                if self.token.encode() in raw:
                    raise ValueError("Credential reflection in response; not saved")
                (self.out / filename).write_bytes(raw)
                item["sha256"] = sha(raw)
                self.manifest["files"][filename] = sha(raw)
                if response.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                    try:
                        retry_delay = max(1, float(response.headers.get("Retry-After", 5 * attempt)))
                    except (ValueError, TypeError):
                        retry_delay = 5 * attempt
                    if not 0 < retry_delay <= 60:
                        raise ValueError("Retry-After exceeds bounded retry window; capture stopped")
                    item["retry_delay_seconds"] = retry_delay
                elif response.status_code != 200:
                    raise ValueError(f"HTTP {response.status_code}; capture stopped without skipping")
                else:
                    body = response.json()
                    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                        raise ValueError("Malformed API response")
                    return body, filename, now()
            except Exception as exc:
                item["error_type"] = type(exc).__name__
                raise
            finally:
                item["finished_utc"] = now()
                self.checkpoint()
            if retry_delay is not None:
                print(f"HTTP {item['status']}: retrying this request in {retry_delay:g}s", flush=True)
                time.sleep(retry_delay)
        raise ValueError("Retry limit exhausted")


def collect_root(client, index, root, root_position, root_count):
    seen_tokens, page, token, count = set(), 0, None, 0
    while True:
        page += 1
        if page > 10000:
            raise ValueError("Pagination safety limit reached")
        params = {"descendants": "true", "subTasks": "true", "pageSize": 1000,
                  "fields": json.dumps(FIELDS)}
        if token is not None:
            params["nextPageToken"] = token
        body, filename, observed = client.fetch(f"folders/{root}/tasks", params)
        if body.get("kind") != "tasks" or len(body["data"]) > 1000:
            raise ValueError("Unexpected task response kind/page size")
        for task in body["data"]:
            index.add(task, root, filename, observed)
        count += len(body["data"])
        next_token = body.get("nextPageToken")
        if next_token is not None and (not isinstance(next_token, str) or not next_token):
            raise ValueError("Malformed pagination token")
        if next_token:
            if not body["data"] or next_token in seen_tokens:
                raise ValueError("Repeated token or empty nonterminal page; capture stopped")
            seen_tokens.add(next_token)
        if page % 10 == 0 or not next_token:
            print(f"Root {root_position}/{root_count}: {page} pages, {count:,} observations; "
                  f"{len(index.tasks):,} unique tasks overall", flush=True)
        if not next_token:
            client.manifest["completed_roots"].append({"root_id": root, "pages": page,
                                                       "task_observations": count})
            client.checkpoint()
            return
        token = next_token
        time.sleep(0.5)


def compare_scope(body, frozen, helper):
    if body.get("kind") != "folderTree" or body.get("nextPageToken"):
        raise ValueError("Unexpected folder-tree response")
    _, current = helper.prepare_scope(body["data"])
    changed = [name for name in ("root_ids", "folder_ids", "folder_structure_sha256")
               if current[name] != frozen[name]]
    return current, changed


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--phase", choices=["before", "after"], required=True)
    parser.add_argument("--source", type=Path, default=Path("Wrike_Data_local_validation.py"))
    args = parser.parse_args()
    helper = load_helper()
    frozen, preparation = read_case(args.case, helper)
    raw_source = args.source.read_bytes()
    if sha(raw_source) != frozen["source_sha256"]:
        raise ValueError("Extraction source differs from preparation; review before any API call")
    tree = ast.parse(raw_source.decode("utf-8-sig"))
    if helper.literal_setting(tree, "SPACE_ID") != frozen["space_id"]:
        raise ValueError("Space differs from prepared scope")
    proxy_mode = preparation["proxy_mode"]
    if proxy_mode not in {"source", "environment", "direct"}:
        raise ValueError("Unexpected proxy mode")
    proxies = helper.literal_setting(tree, "PROXIES") if proxy_mode == "source" else {}
    if not isinstance(proxies, dict) or any(k not in {"http", "https"} or not isinstance(v, str)
                                          for k, v in proxies.items()):
        raise ValueError("Invalid proxy setting")
    import requests
    token = getpass.getpass(f"Paste the SAME {frozen['label']} token used for preparation (hidden): ").strip()
    if not token:
        raise ValueError("Empty token")
    session = requests.Session()
    session.trust_env = proxy_mode == "environment"
    session.proxies.update(proxies)
    session.headers.update({"Authorization": "Bearer " + token, "Cache-Control": "no-cache"})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    out = args.case / f"reference_{args.phase}_{stamp}"
    (out / "responses").mkdir(parents=True, exist_ok=False)
    client = Capture(out, session, token, {
        "phase": args.phase, "label": frozen["label"], "space_id": frozen["space_id"],
        "source_sha256": frozen["source_sha256"], "capture_script_sha256": sha(Path(__file__).read_bytes()),
        "frozen_scope_sha256": sha((args.case / "frozen_scope.json").read_bytes()),
        "token_identity": "operator-supplied same-token requirement; no credential fingerprint retained",
        "scope": "independent descendant/subtask queries of frozen roots; no status filter",
        "not_an_atomic_snapshot": True,
    })
    index = TaskIndex()
    error = None
    try:
        body, _, _ = client.fetch(f"spaces/{frozen['space_id']}/folders")
        current, changed = compare_scope(body, frozen, helper)
        client.save("scope_at_start.json", current)
        if changed:
            raise ValueError("Folder scope changed since preparation: " + ", ".join(changed))
        roots = frozen["root_ids"]
        for position, root in enumerate(roots, 1):
            collect_root(client, index, root, position, len(roots))
            time.sleep(0.5)
        body, _, _ = client.fetch(f"spaces/{frozen['space_id']}/folders")
        current, changed = compare_scope(body, frozen, helper)
        client.save("scope_at_end.json", current)
        if sha(args.source.read_bytes()) != frozen["source_sha256"]:
            raise ValueError("Extraction source changed during reference capture")
        summary = index.summarize()
        reasons = []
        if changed:
            reasons.append("folder_scope_changed_during_capture")
        for name in ("conflicting_task_ids", "missing_relationship_task_ids", "missing_timestamp_task_ids",
                     "unexpected_task_scope_ids", "referenced_subtask_ids_not_returned",
                     "repeated_task_observations_within_root"):
            if summary[name]:
                reasons.append(name)
        allowed = {"MODE_NONE_WITHOUT_TOTAL", "EXPLICIT_ZERO", "EXPLICIT_POSITIVE", "CONFLICTING_OBSERVATIONS"}
        if set(summary["effort_categories"]) - allowed:
            reasons.append("unresolved_effort_shapes")
        if not index.tasks:
            reasons.append("empty_reference_requires_review")
        client.manifest["status"] = "CAPTURE_COMPLETE_REVIEW_REQUIRED" if reasons else "CAPTURE_COMPLETE_NOT_COMPARED"
        summary.update({"status": client.manifest["status"], "review_reasons": reasons,
                        "completed_roots": len(client.manifest["completed_roots"]),
                        "scope_changes": changed,
                        "mode_none_policy": "kept separate from explicit numeric zero; no numeric value imputed",
                        "validation_complete": False})
        client.save("summary.json", summary)
    except Exception as exc:
        client.manifest["status"] = "CAPTURE_BLOCKED"
        client.manifest["error_type"] = type(exc).__name__
        error = str(exc) if type(exc) is ValueError else type(exc).__name__
        if token in error:
            error = "Error details omitted"
        client.manifest["reason"] = error
        summary = {"status": "CAPTURE_BLOCKED", "reason": error,
                   "partial_unique_tasks": len(index.tasks), "validation_complete": False}
        client.save("summary.json", summary)
    finally:
        index.save(out / "tasks.jsonl")
        digest = hashlib.sha256()
        with (out / "tasks.jsonl").open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        client.manifest["files"]["tasks.jsonl"] = digest.hexdigest()
        client.manifest["finished_utc"] = now()
        client.checkpoint()
        session.close()
    print("\nStatus:", client.manifest["status"])
    if error:
        print("Reason:", error)
        print("Partial unique tasks:", len(index.tasks))
    else:
        print("Completed roots:", summary["completed_roots"])
        print("Unique tasks:", summary["unique_tasks"])
        print("Effort categories:", json.dumps(summary["effort_categories"], sort_keys=True))
        print("Explicit numeric effort hours (not a validated total):", summary["explicit_numeric_hours"])
        print("Review reasons:", ", ".join(summary["review_reasons"]) or "none detected")
    print("Evidence:", out)
    print("This capture has NOT been compared with a new extraction. No external writes.")
    return 2 if error else (3 if summary["review_reasons"] else 0)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, SyntaxError) as exc:
        message = str(exc) if type(exc) is ValueError else type(exc).__name__
        print("Capture could not start:", message)
        raise SystemExit(2)
