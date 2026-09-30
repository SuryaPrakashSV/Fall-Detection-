#!/usr/bin/env python3
"""Read-only current scope probe. Never imports/runs the extraction source.

Uses one space folder-tree GET and one GET for each of two known tasks.
With --reuse-folder-tree, verifies/reuses the last saved tree and makes two GETs.
Writes only a new local evidence directory. No Snowflake connection or writes.
This is NOT a full task extraction or historical completeness validation.
"""
import argparse
import ast
from collections import defaultdict, deque
from datetime import datetime, timezone
import getpass
import hashlib
import json
from pathlib import Path
import sys

TASK_IDS = ("MAAAAAEPJzKc", "MAAAAAEQ0PI6")


def now():
    return datetime.now(timezone.utc).isoformat()


def literal_setting(tree, name):
    values = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
            if value not in values:
                values.append(value)
    if len(values) != 1:
        raise ValueError(f"Cannot unambiguously read literal {name}; no source was executed")
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True, choices=["surya", "akash"])
    parser.add_argument("--source", default="Wrike_Data_local_validation.py")
    parser.add_argument("--reuse-folder-tree", action="store_true",
                        help="Reuse the latest verified folder inventory for this label; fetch only two tasks")
    parser.add_argument("--proxy-mode", choices=["source", "environment", "direct"], default="source")
    args = parser.parse_args()
    source = Path(args.source).read_bytes()
    tree = ast.parse(source.decode("utf-8-sig"))
    space_id = literal_setting(tree, "SPACE_ID")
    if not isinstance(space_id, str) or not space_id.isalnum():
        raise ValueError("Unexpected SPACE_ID")
    proxies = literal_setting(tree, "PROXIES") if args.proxy_mode == "source" else None
    if proxies is not None and (not isinstance(proxies, dict) or
            any(k not in ("http", "https") or not isinstance(v, str) for k, v in proxies.items())):
        raise ValueError("Unexpected PROXIES setting")
    reused = None
    if args.reuse_folder_tree:
        runs = sorted(p for p in Path("output/folder_scope_probe").glob(f"*_{args.label}")
                      if (p / "folder_tree.json").is_file() and (p / "manifest.json").is_file())
        if not runs:
            raise ValueError("No saved folder tree found for this label")
        previous = runs[-1]
        prior_manifest = json.loads((previous / "manifest.json").read_text())
        if prior_manifest.get("space_id") != space_id or prior_manifest.get("label") != args.label:
            raise ValueError("Saved inventory space/label does not match")
        if prior_manifest.get("source_sha256") != hashlib.sha256(source).hexdigest():
            raise ValueError("Extraction source changed since saved folder inventory")
        capture = next((r for r in prior_manifest.get("requests", [])
                        if r.get("endpoint") == f"spaces/{space_id}/folders"
                        and r.get("status") == 200 and r.get("response_file") == "folder_tree.json"), None)
        raw_tree = (previous / "folder_tree.json").read_bytes()
        if capture is None or hashlib.sha256(raw_tree).hexdigest() != capture.get("sha256"):
            raise ValueError("Saved folder-tree hash/capture could not be verified")
        reused = {"path": str(previous / "folder_tree.json"),
                  "sha256": capture["sha256"], "capture_started_utc": capture["started_utc"]}
        reused_body = json.loads(raw_tree)
    import requests
    token = getpass.getpass(f"Paste {args.label}'s Wrike token (hidden): ").strip()
    if not token:
        raise ValueError("Empty token")
    session = requests.Session()
    session.trust_env = args.proxy_mode == "environment"
    session.headers.update({"Authorization": f"Bearer {token}", "Cache-Control": "no-cache"})
    if proxies:
        session.proxies.update(proxies)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    out = Path("output/folder_scope_probe") / f"{stamp}_{args.label}"
    out.mkdir(parents=True, exist_ok=False)
    manifest = {"started_utc": now(), "label": args.label, "space_id": space_id,
                "source_sha256": hashlib.sha256(source).hexdigest(),
                "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "proxy_mode": args.proxy_mode, "requests": []}
    if reused:
        manifest["reused_folder_tree"] = reused

    def save(name, value):
        (out / name).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")

    def fetch(endpoint, filename, params=None):
        entry = {"endpoint": endpoint, "params": params, "started_utc": now()}
        manifest["requests"].append(entry)
        try:
            response = session.get("https://www.wrike.com/api/v4/" + endpoint,
                                   params=params, timeout=(15, 60), allow_redirects=False)
            entry["status"] = response.status_code
            raw = response.content
            (out / filename).write_bytes(raw)
            entry.update({"response_file": filename, "sha256": hashlib.sha256(raw).hexdigest(),
                          "server_date": response.headers.get("Date")})
            if response.status_code != 200:
                raise ValueError(f"HTTP {response.status_code}; saved response; no retry")
            body = response.json()
            if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                raise ValueError("Unexpected response shape")
            return body
        except Exception as exc:
            entry["error_type"] = type(exc).__name__
            raise
        finally:
            entry["finished_utc"] = now()
            save("manifest.json", manifest)

    try:
        body = reused_body if reused else fetch(f"spaces/{space_id}/folders", "folder_tree.json")
        if body.get("kind") != "folderTree" or body.get("nextPageToken"):
            raise ValueError("Unexpected folder-tree mode/pagination; stop for review")
        result = analyse(body["data"])
        result["response_kind"] = body.get("kind")
        result["tasks"] = []
        for task_id in TASK_IDS:
            item = {"requested_id": task_id}
            try:
                task_body = fetch(f"tasks/{task_id}", f"task_{task_id}.json",
                                  {"fields": json.dumps(["effortAllocation"])})
                item["records"] = task_body["data"]
                item["direct_parents_in_modeled_folder_scope"] = sorted({
                    p for row in task_body["data"] for p in row.get("parentIds", [])
                    if p in result["within_four_levels_ids"]})
            except Exception as exc:
                item["error_type"] = type(exc).__name__
                item["http_status"] = manifest["requests"][-1].get("status")
            result["tasks"].append(item)
        save("scope_analysis.json", result)
        counts = {k: len(result[k]) for k in (
            "multiple_parents", "original_selected_root_ids", "extra_roots_from_title_match",
            "lost_roots_from_title_match", "all_parent_variant_added_roots",
            "all_parent_variant_removed_roots", "within_four_levels_ids",
            "beyond_four_levels_ids", "selected_closure_ids_absent_from_inventory")}
        brief = {"label": args.label, "kind": result["response_kind"],
                 "reused_folder_tree": reused,
                 "folder_records": result["folder_records"], "counts": counts,
                 "tasks": [{"requested_id": t["requested_id"],
                            "http_status": t.get("http_status", 200),
                            "error_type": t.get("error_type"),
                            "returned_ids": [r.get("id") for r in t.get("records", [])],
                            "scopes": [r.get("scope") for r in t.get("records", [])],
                            "createdDates": [r.get("createdDate") for r in t.get("records", [])],
                            "updatedDates": [r.get("updatedDate") for r in t.get("records", [])],
                            "effortAllocations": [r.get("effortAllocation") for r in t.get("records", [])],
                            "parentIds": [r.get("parentIds", []) for r in t.get("records", [])],
                            "superTaskIds": [r.get("superTaskIds", []) for r in t.get("records", [])],
                            "direct_parents_in_modeled_folder_scope": t.get("direct_parents_in_modeled_folder_scope", [])}
                           for t in result["tasks"]],
                 "limitation": "Current scope probe only; historical causes and full task completeness remain unvalidated."}
        save("summary.json", brief)
        print(json.dumps(brief, indent=2))
    finally:
        manifest["finished_utc"] = now()
        save("manifest.json", manifest)
        session.close()
        print(f"Evidence saved: {out}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"STOPPED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
