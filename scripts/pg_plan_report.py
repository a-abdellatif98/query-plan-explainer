#!/usr/bin/env python3
"""Report the slowest nodes and worst row estimates in a PostgreSQL EXPLAIN (ANALYZE, FORMAT JSON) plan."""

import argparse
import json
import re
import sys

TOP_N = 3
MIN_MISS_FACTOR = 10.0


class PlanError(ValueError):
    pass


def strip_psql_decoration(text):
    lines = []
    for line in text.splitlines():
        stripped = line.rstrip()
        if re.fullmatch(r"\s*(QUERY PLAN|-+|\(\d+ rows?\))\s*", stripped):
            continue
        lines.append(re.sub(r"\s*\+$", "", stripped))
    return "\n".join(lines)


def load_plan(text):
    for candidate in (text, strip_psql_decoration(text)):
        try:
            data = json.loads(candidate)
            break
        except json.JSONDecodeError:
            continue
    else:
        raise PlanError("input is not JSON; run EXPLAIN (ANALYZE, FORMAT JSON) and paste the whole result")
    if isinstance(data, list):
        if not data:
            raise PlanError("empty plan list")
        data = data[0]
    if not isinstance(data, dict) or "Plan" not in data:
        raise PlanError('no top-level "Plan" key; this does not look like PostgreSQL FORMAT JSON output')
    return data


def label(node):
    text = node.get("Node Type", "?")
    if node.get("Join Type"):
        text = f"{text} ({node['Join Type']})"
    if node.get("Relation Name"):
        text += f" on {node['Relation Name']}"
        if node.get("Alias") and node["Alias"] != node["Relation Name"]:
            text += f" {node['Alias']}"
    if node.get("Index Name"):
        text += f" using {node['Index Name']}"
    if node.get("Subplan Name"):
        text += f" [{node['Subplan Name']}]"
    return text


def total_time(node, processes=1):
    return node.get("Actual Total Time", 0.0) * node.get("Actual Loops", 0) / processes


def number(value):
    return f"{int(value)}" if float(value).is_integer() else f"{value:.2f}"


def own_temp_written(node):
    children = sum(child.get("Temp Written Blocks", 0) for child in node.get("Plans", []))
    return node.get("Temp Written Blocks", 0) - children


def flags(node):
    found = []
    if node.get("Sort Space Type") == "Disk":
        found.append(f"sort spilled to disk ({node.get('Sort Method', '?')}, {node.get('Sort Space Used', '?')} kB)")
    if node.get("Hash Batches", 1) > 1:
        found.append(f"hash split into {node['Hash Batches']} batches (spilled to disk)")
    if node.get("HashAgg Batches", 0) > 1:
        found.append(f"hash aggregate split into {node['HashAgg Batches']} batches (spilled to disk)")
    removed = node.get("Rows Removed by Filter", 0)
    kept = node.get("Actual Rows", 0)
    if removed and removed >= 10 * max(kept, 1):
        found.append(f"filter discarded {number(removed)} rows per loop to keep {number(kept)}")
    if node.get("Rows Removed by Join Filter", 0) >= 10 * max(kept, 1):
        found.append(f"join filter discarded {number(node['Rows Removed by Join Filter'])} rows per loop")
    if node.get("Lossy Heap Blocks", 0) > 0:
        found.append(f"{node['Lossy Heap Blocks']} lossy heap blocks (bitmap exceeded work_mem)")
    returned = kept * node.get("Actual Loops", 0)
    if node.get("Node Type") == "Index Only Scan" and node.get("Heap Fetches", 0) >= 0.1 * max(returned, 1):
        found.append(f"{node['Heap Fetches']} heap fetches for {number(returned)} rows (visibility map not current, consider VACUUM)")
    if node.get("Node Type") == "Nested Loop" and node.get("Plans"):
        inner_loops = node["Plans"][-1].get("Actual Loops", 0)
        if inner_loops >= 1000:
            found.append(f"inner side executed {inner_loops} times")
    if own_temp_written(node) > 0:
        found.append(f"wrote {own_temp_written(node)} temp blocks")
    return found


def walk(node, depth=0, processes=1, under_limit=False):
    children = node.get("Plans", [])
    child_processes = processes
    if node.get("Node Type") in ("Gather", "Gather Merge"):
        child_processes = node.get("Workers Launched", 0) + 1
    inclusive = total_time(node, processes)
    exclusive = inclusive - sum(total_time(child, child_processes) for child in children)
    yield {
        "node": label(node),
        "depth": depth,
        "inclusive_ms": round(inclusive, 3),
        "exclusive_ms": round(max(exclusive, 0.0), 3),
        "loops": node.get("Actual Loops", 0),
        "plan_rows": node.get("Plan Rows"),
        "actual_rows": node.get("Actual Rows"),
        "parallel": processes > 1,
        "under_limit": under_limit,
        "timed": "Actual Total Time" in node,
        "flags": flags(node),
    }
    for child in children:
        yield from walk(child, depth + 1, child_processes, under_limit or node.get("Node Type") == "Limit")


def miss_factor(entry):
    estimated = max(entry["plan_rows"] or 0, 1)
    actual = max(entry["actual_rows"] or 0, 1)
    return max(estimated, actual) / min(estimated, actual), "under" if actual > estimated else "over"


def analyze(plan, top=TOP_N, min_factor=MIN_MISS_FACTOR):
    nodes = list(walk(plan["Plan"]))
    if all(n["actual_rows"] is None for n in nodes):
        raise PlanError("plan has no actual row counts; run EXPLAIN with ANALYZE")
    has_timing = any(n["timed"] for n in nodes)
    total = plan.get("Execution Time") or (nodes[0]["inclusive_ms"] if has_timing else None)
    for n in nodes:
        n["percent"] = round(100.0 * n["exclusive_ms"] / total, 1) if has_timing and total else None
    slowest = sorted(nodes, key=lambda n: n["exclusive_ms"], reverse=True)[:top] if has_timing else []
    misses = []
    for n in nodes:
        if n["loops"] == 0 or n["plan_rows"] is None or n["actual_rows"] is None:
            continue
        factor, direction = miss_factor(n)
        if direction == "over" and n["under_limit"]:
            continue
        if factor >= min_factor:
            misses.append(dict(n, factor=round(factor, 1), direction=direction))
    misses.sort(key=lambda n: n["factor"], reverse=True)
    return {
        "execution_ms": plan.get("Execution Time"),
        "planning_ms": plan.get("Planning Time"),
        "has_timing": has_timing,
        "slowest": slowest,
        "estimate_misses": misses[:top],
        "flagged": [n for n in nodes if n["flags"]],
    }


def render(report):
    out = []
    if report["execution_ms"] is not None:
        out.append(f"Execution time: {report['execution_ms']:.3f} ms (planning {report['planning_ms'] or 0:.3f} ms)")
    out.append("")
    out.append("Top nodes by exclusive time:")
    if not report["has_timing"]:
        out.append("  (no timings: plan was run with TIMING OFF)")
    for i, n in enumerate(report["slowest"], 1):
        pct = f" ({n['percent']}%)" if n["percent"] is not None else ""
        note = " [parallel: time averaged across processes]" if n["parallel"] else ""
        out.append(f"  {i}. {n['node']}: {n['exclusive_ms']:.3f} ms{pct}, loops={n['loops']}{note}")
    out.append("")
    out.append("Worst row-estimate misses:")
    if not report["estimate_misses"]:
        out.append("  none at or above the threshold")
    for i, n in enumerate(report["estimate_misses"], 1):
        out.append(
            f"  {i}. {n['node']}: estimated {number(n['plan_rows'])}, actual {number(n['actual_rows'])} per loop "
            f"({n['factor']:g}x {n['direction']}estimate)"
        )
    if report["flagged"]:
        out.append("")
        out.append("Warnings:")
        for n in report["flagged"]:
            for flag in n["flags"]:
                out.append(f"  - {n['node']}: {flag}")
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", help="plan file (default: stdin)")
    parser.add_argument("--top", type=int, default=TOP_N)
    parser.add_argument("--min-factor", type=float, default=MIN_MISS_FACTOR)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    try:
        text = open(args.path, encoding="utf-8").read() if args.path else sys.stdin.read()
        report = analyze(load_plan(text), args.top, args.min_factor)
    except (PlanError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
