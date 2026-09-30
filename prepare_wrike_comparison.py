#!/usr/bin/env python3
"""Prepare a bounded Wrike extraction comparison; this is NOT the comparison.

Run from ~/Desktop/wrike-local-baseline:
    python3 prepare_wrike_comparison.py --label surya

Makes at most two read-only GET requests: one current space folder tree and
one page of at most one task under a selected root. No retries, SQL, source
execution, full extraction, token storage or external writes. New local
output only. Does not change either historical report or production code.

The frozen scope models the reviewed extractor's root-selection rules. This
is a declared comparison scope, not proof that the business scope is complete.
Reference task discovery will use folder descendant/subtask searches, never
seeded with task IDs from the extraction under test. Before/after captures
are required around a NEW extraction using the SAME token. Historical CSVs
cannot serve as the aligned extraction. Endpoint samples do not validate all
records; the two historical omissions and deletion dates are separate limits.

Official parameters verified 2026-09-30:
https://developers.wrike.com/reference/getfolderssingletasks
https://developers.wrike.com/reference/getspacessinglefolders
"""
import argparse
import ast
from collections import defaultdict, deque
from datetime import datetime, timezone
import getpass
import hashlib
import json
from pathlib import Path
import re
import sys


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def literal_setting(tree, name):
    values = []
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else [])
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            if isinstance(node, ast.AugAssign):
                raise ValueError(f"Dynamic {name}; stop for review")
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                raise ValueError(f"Nonliteral {name}; stop for review") from None
            if value not in values:
                values.append(value)
    if len(values) != 1:
        raise ValueError(f"Ambiguous or missing {name}; stop for review")
    return values[0]


def analyse(rows):
    folder_map = {}
    all_parents = defaultdict(set)
    last_parent = {}
    duplicate_ids = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise ValueError("Invalid folder record")
        fid = row["id"]
        if fid in folder_map:
            duplicate_ids.add(fid)
        folder_map[fid] = row
        children = row.get("childIds", [])
        if not isinstance(children, list) or any(not isinstance(x, str) for x in children):
            raise ValueError("Invalid childIds")
        for child in children:
            all_parents[child].add(fid)
            last_parent[child] = fid

    dcm_ids = [r["id"] for r in rows
               if r.get("title", "").strip().lower() == "dcm projects"]
    dcm_children = set(folder_map[dcm_ids[0]].get("childIds", [])) if dcm_ids else set()

    def under_project(fid, use_all):
        pending = list(all_parents.get(fid, ())) if use_all else (
            [last_parent[fid]] if fid in last_parent else [])
        seen = {fid}
        while pending:
            parent = pending.pop()
            if parent in seen:
                if not use_all:
                    raise ValueError("Cycle in single-parent chain; original selection could hang")
                continue
            seen.add(parent)
            if "project" in folder_map.get(parent, {}):
                return True
            pending.extend(all_parents.get(parent, ()) if use_all else (
                [last_parent[parent]] if parent in last_parent else []))
        return False

    def selected_roots(use_all=False):
        candidates = set()
        for row in rows:
            fid = row["id"]
            title = row.get("title", "").strip()
            if not title or "project" in row or title.lower() == "dcm projects":
                continue
            if fid in dcm_children or (row.get("scope") == "WsFolder"
                    and not under_project(fid, use_all) and bool(row.get("childIds"))):
                candidates.add(fid)
        selected = set()
        for fid in candidates:
            parents = all_parents.get(fid, set()) if use_all else (
                {last_parent[fid]} if fid in last_parent else set())
            if fid in dcm_children or not parents or not any(
                p in candidates and folder_map.get(p, {}).get("title", "").lower() != "dcm projects"
                for p in parents
            ):
                selected.add(fid)
        return selected

    def walk(roots, limit=None):
        distances = {fid: 0 for fid in roots}
        queue = deque(roots)
        while queue:
            fid = queue.popleft()
            depth = distances[fid]
            if limit is not None and depth >= limit:
                continue
            for child in folder_map.get(fid, {}).get("childIds", []):
                if child not in distances:
                    distances[child] = depth + 1
                    queue.append(child)
        return distances

    original = selected_roots()
    all_parent_roots = selected_roots(True)
    names = {folder_map[fid]["title"].strip() for fid in original}
    title_roots = {fid for fid, row in folder_map.items()
                   if row.get("title") in names and row.get("title", "").lower() != "dcm projects"}
    limited = walk(title_roots, 4)
    full = walk(title_roots)
    title_groups = defaultdict(list)
    for fid, row in folder_map.items():
        if row.get("title") in names:
            title_groups[row["title"]].append(fid)
    result = {
        "folder_records": len(rows), "unique_folder_ids": len(folder_map),
        "duplicate_folder_ids": sorted(duplicate_ids), "dcm_folder_ids": dcm_ids,
        "multiple_parents": {k: sorted(v) for k, v in all_parents.items() if len(v) > 1},
        "original_selected_root_ids": sorted(original),
        "roots_selected_by_later_title_match": sorted(title_roots),
        "extra_roots_from_title_match": sorted(title_roots - original),
        "lost_roots_from_title_match": sorted(original - title_roots),
        "duplicate_selected_titles": {k: sorted(v) for k, v in title_groups.items() if len(v) > 1},
        "all_parent_variant_added_roots": sorted(all_parent_roots - original),
        "all_parent_variant_removed_roots": sorted(original - all_parent_roots),
        "within_four_levels_ids": sorted(limited),
        "unrestricted_descendant_ids": sorted(full),
        "beyond_four_levels_ids": sorted(set(full) - set(limited)),
        "referenced_ids_absent_from_inventory": sorted(set(all_parents) - set(folder_map)),
        "selected_closure_ids_absent_from_inventory": sorted(set(full) - set(folder_map)),
        "notes": [
            "Both selection variants use the first DCM Projects match, matching inspected code.",
            "All-parent variant is a sensitivity check, not an approved business scope.",
            "One current inventory models both original inventory calls; timing drift and worker failures are not reproduced.",
            "Depth findings concern folder enumeration, not proven task omissions.",
            "No historical deletion dates or extraction completeness are validated.",
        ],
    }
    return result


def folder_signature(rows):
    projection = [
        {"id": r["id"], "title": r.get("title"), "scope": r.get("scope"),
         "has_project": "project" in r, "childIds": sorted(r.get("childIds", []))}
        for r in rows
    ]
    return sha(json.dumps(sorted(projection, key=lambda r: r["id"]),
                          sort_keys=True, separators=(",", ":")).encode())


def prepare_scope(rows):
    analysis = analyse(rows)
    blockers = [key for key in (
        "duplicate_folder_ids", "lost_roots_from_title_match",
        "selected_closure_ids_absent_from_inventory", "beyond_four_levels_ids"
    ) if analysis[key]]
    roots = analysis["roots_selected_by_later_title_match"]
    if not roots:
        blockers.append("empty_root_scope")
    if blockers:
        raise ValueError("Scope requires review: " + ", ".join(blockers))
    # Root identity is frozen explicitly; never infer task membership from titles.
    mapping = {r["id"]: r for r in rows}
    def closure_size(root):
        seen, queue = set(), [root]
        while queue:
            fid = queue.pop()
            if fid in seen:
                continue
            seen.add(fid)
            queue.extend(mapping[fid].get("childIds", []))
        return len(seen)
    sample_root = sorted(roots, key=lambda r: (-closure_size(r), r))[0]
    return analysis, {
        "root_ids": roots,
        "root_titles": {r: mapping[r].get("title", "") for r in roots},
        "folder_ids": analysis["unrestricted_descendant_ids"],
        "folder_structure_sha256": folder_signature(rows),
        "sample_root_id": sample_root,
        "selection_basis": "reviewed extractor root/title-selection model, frozen as IDs",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", choices=["surya", "akash"], default="surya")
    parser.add_argument("--source", default="Wrike_Data_local_validation.py")
    parser.add_argument("--proxy-mode", choices=["source", "environment", "direct"],
                        default="source")
    args = parser.parse_args()
    source_path = Path(args.source)
    raw_source = source_path.read_bytes()
    tree = ast.parse(raw_source.decode("utf-8-sig"))
    space = literal_setting(tree, "SPACE_ID")
    if not isinstance(space, str) or not re.fullmatch(r"[A-Za-z0-9]+", space):
        raise ValueError("Unexpected SPACE_ID")
    proxies = literal_setting(tree, "PROXIES") if args.proxy_mode == "source" else {}
    if not isinstance(proxies, dict) or any(
        k not in {"http", "https"} or not isinstance(v, str) for k, v in proxies.items()
    ):
        raise ValueError("Unexpected PROXIES")
    import requests
    token = getpass.getpass(f"Paste {args.label}'s Wrike token (hidden): ").strip()
    if not token:
        raise ValueError("No token entered")
    session = requests.Session()
    session.trust_env = args.proxy_mode == "environment"
    session.proxies.update(proxies)
    session.headers.update({"Authorization": "Bearer " + token, "Cache-Control": "no-cache"})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    out = Path("output/aligned_comparison") / (stamp + "_" + args.label)
    out.mkdir(parents=True, exist_ok=False)
    manifest = {"status": "IN_PROGRESS", "started_utc": now(), "label": args.label,
                "space_id": space, "source_sha256": sha(raw_source),
                "preflight_sha256": sha(Path(__file__).read_bytes()), "requests": [],
                "files": {}, "proxy_mode": args.proxy_mode}
    def save(name, value):
        raw = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()
        (out / name).write_bytes(raw)
        if name != "manifest.json":
            manifest["files"][name] = sha(raw)
    def fetch(endpoint, name, params=None):
        item = {"endpoint": endpoint, "params": params, "started_utc": now()}
        manifest["requests"].append(item)
        try:
            response = session.get("https://www.wrike.com/api/v4/" + endpoint,
                                   params=params, timeout=(15, 60), allow_redirects=False)
            item.update({"status": response.status_code,
                         "server_date": response.headers.get("Date")})
            raw = response.content
            # Avoid writing a credential even if an unexpected server reflects it.
            if token.encode() in raw:
                raise ValueError("Unexpected credential reflection; response not saved")
            (out / name).write_bytes(raw)
            item.update({"response_file": name, "sha256": sha(raw)})
            manifest["files"][name] = sha(raw)
            if response.status_code != 200:
                raise ValueError(f"HTTP {response.status_code}; response saved; no retry")
            body = response.json()
            if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                raise ValueError("Unexpected API response shape")
            return body
        finally:
            item["finished_utc"] = now()
            save("manifest.json", manifest)
    error = None
    try:
        body = fetch(f"spaces/{space}/folders", "folder_tree.json")
        if body.get("kind") != "folderTree" or body.get("nextPageToken"):
            raise ValueError("Unexpected folder response kind or pagination")
        analysis, scope = prepare_scope(body["data"])
        save("scope_analysis.json", analysis)
        scope.update({"label": args.label, "space_id": space,
                      "source_sha256": manifest["source_sha256"], "prepared_utc": now()})
        save("frozen_scope.json", scope)
        params = {"descendants": "true", "subTasks": "true", "pageSize": 1,
                  "fields": json.dumps(["effortAllocation", "parentIds", "superParentIds",
                                        "superTaskIds", "subTaskIds"])}
        sample = fetch(f"folders/{scope['sample_root_id']}/tasks", "endpoint_sample.json", params)
        if sample.get("kind") != "tasks" or len(sample["data"]) > 1:
            raise ValueError("Unexpected task kind or page-size behavior")
        if not sample["data"]:
            raise ValueError("Selected root returned no sample task; review before full capture")
        task = sample["data"][0]
        if not isinstance(task, dict) or not isinstance(task.get("id"), str) or not task["id"]:
            raise ValueError("Malformed sample task")
        required = {"effortAllocation", "parentIds", "superParentIds", "superTaskIds", "subTaskIds"}
        missing = required - task.keys()
        if missing:
            raise ValueError("Sample omitted requested fields: " + ", ".join(sorted(missing)))
        for field in required - {"effortAllocation"}:
            if not isinstance(task[field], list) or any(not isinstance(x, str) for x in task[field]):
                raise ValueError("Unexpected relationship field shape")
        if task["effortAllocation"] is not None and not isinstance(task["effortAllocation"], dict):
            raise ValueError("Unexpected effortAllocation shape")
        if sha(source_path.read_bytes()) != manifest["source_sha256"]:
            raise ValueError("Extraction source changed during preflight")
        specification = {
            "status": "PREPARED_NOT_VALIDATED", "same_token_required": True,
            "scope": "union of frozen root descendants and their tasks/subtasks; all statuses",
            "metric": "task effortAllocation.totalEffort in minutes; divide by 60 for hours",
            "task_identity": "CSV key equals deepest populated task hierarchy ID; exclude folder own rows",
            "reference_discovery": "independent paginated GET folders/{rootId}/tasks with descendants=true and subTasks=true",
            "independence_rule": "reference search must not be seeded with extractor task IDs",
            "sequence": ["independent before capture with scope checks",
                         "record start and run new CSV-only extraction with the same token",
                         "record end and hash the new CSV",
                         "independent after capture with scope checks",
                         "compare exact task sets and per-task effort, then reconcile each discrepancy"],
            "gates": ["all pages completed; repeated tokens and malformed responses fail",
                      "every failed request blocks a complete result",
                      "no silent assumption that an absent effort field means zero",
                      "conflicting repeated task values retained as evidence",
                      "folder scope and source hashes checked before and after",
                      "changed IDs, effort, hierarchy and update timestamps flagged for review",
                      "all discrepant IDs receive a classification supported by evidence"],
            "limits": ["API pagination is not an atomic historical snapshot",
                       "equal before/after observations cannot rule out intervening changes",
                       "same-token validation cannot prove access to unseen tasks",
                       "new validation does not establish historical deletion dates or explain September 24 omissions",
                       "a successful comparison supports consistency within the declared scope/window, not an unqualified 100% claim"],
            "endpoint_documentation": ["https://developers.wrike.com/reference/getfolderssingletasks",
                                       "https://developers.wrike.com/reference/getspacessinglefolders"],
        }
        save("comparison_specification.json", specification)
        manifest["status"] = "PREPARED_NOT_VALIDATED"
        result = {"status": manifest["status"], "root_count": len(scope["root_ids"]),
                  "folder_count": len(scope["folder_ids"]), "sample_task_count": 1,
                  "sample_has_totalEffort": isinstance(task["effortAllocation"], dict)
                      and "totalEffort" in task["effortAllocation"],
                  "full_extraction_started": False, "external_writes": False}
        save("summary.json", result)
    except Exception as exc:
        manifest["status"] = "BLOCKED"
        manifest["error_type"] = type(exc).__name__
        # Only our own ValueError messages are displayed; do not echo proxy/URL credentials.
        error = str(exc) if type(exc) is ValueError else type(exc).__name__
        if token in error:
            error = "Error text omitted"
        manifest["reason"] = error
        save("summary.json", {"status": "BLOCKED", "reason": error})
    finally:
        session.close()
        manifest["finished_utc"] = now()
        save("manifest.json", manifest)
    print("Status:", manifest["status"])
    if error:
        print("Reason:", error)
    else:
        print("Frozen roots:", result["root_count"])
        print("Frozen folders:", result["folder_count"])
        print("Endpoint sample: 1 task; requested fields present")
        print("Sample has totalEffort:", result["sample_has_totalEffort"])
    print("Evidence:", out)
    print("No full extraction started. No Snowflake or Wrike writes.")
    return 2 if error else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, SyntaxError) as exc:
        print("Preflight stopped before completion:", type(exc).__name__)
        raise SystemExit(2)
