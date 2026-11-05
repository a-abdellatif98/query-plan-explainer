import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

spec = importlib.util.spec_from_file_location("pg_plan_report", ROOT / "scripts" / "pg_plan_report.py")
report_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report_module)


def report_for(name, **kwargs):
    return report_module.analyze(report_module.load_plan((FIXTURES / name).read_text()), **kwargs)


def names(entries):
    return [entry["node"] for entry in entries]


def test_sort_spill_ranks_seq_scan_then_sort():
    report = report_for("pg_sort_spill.json")
    assert names(report["slowest"]) == ["Seq Scan on orders", "Sort", "Limit"]
    assert [n["exclusive_ms"] for n in report["slowest"]] == [410.0, 402.4, 0.1]
    assert report["slowest"][0]["percent"] == 50.3


def test_sort_spill_flags_disk_sort_and_wasteful_filter():
    flags = {n["node"]: n["flags"] for n in report_for("pg_sort_spill.json")["flagged"]}
    assert "sort spilled to disk (external merge, 45000 kB)" in flags["Sort"]
    assert flags["Seq Scan on orders"] == ["filter discarded 4880000 rows per loop to keep 120000"]


def test_temp_blocks_are_attributed_to_the_node_that_wrote_them():
    flags = {n["node"]: n["flags"] for n in report_for("pg_sort_spill.json")["flagged"]}
    assert "Limit" not in flags
    assert "wrote 5630 temp blocks" in flags["Sort"]


def test_overestimate_under_limit_is_not_reported():
    assert report_for("pg_sort_spill.json")["estimate_misses"] == []


def test_nested_loop_inner_time_multiplied_by_loops():
    report = report_for("pg_nested_loop_misestimate.json")
    top = report["slowest"][0]
    assert top["node"] == "Index Scan on orders o using orders_customer_id_idx"
    assert top["exclusive_ms"] == 2300.0
    assert top["loops"] == 20000
    assert report["slowest"][1]["exclusive_ms"] == 65.0


def test_nested_loop_misestimates_ranked_by_factor():
    misses = report_for("pg_nested_loop_misestimate.json")["estimate_misses"]
    assert [(m["node"], m["factor"], m["direction"]) for m in misses] == [
        ("Nested Loop (Inner)", 12000.0, "under"),
        ("Seq Scan on customers c", 4000.0, "under"),
    ]


def test_nested_loop_with_many_inner_loops_is_flagged():
    flags = {n["node"]: n["flags"] for n in report_for("pg_nested_loop_misestimate.json")["flagged"]}
    assert flags["Nested Loop (Inner)"] == ["inner side executed 20000 times"]


def test_psql_decorated_output_is_parsed():
    report = report_for("pg_parallel_hash_psql.txt")
    assert report["execution_ms"] == 905.0


def test_parallel_time_is_averaged_over_processes():
    report = report_for("pg_parallel_hash_psql.txt")
    assert names(report["slowest"]) == ["Hash Join (Inner)", "Seq Scan on events e", "Hash"]
    assert [n["exclusive_ms"] for n in report["slowest"]] == [350.0, 300.0, 110.0]
    assert all(n["parallel"] for n in report["slowest"])
    assert all(n["percent"] <= 100 for n in report["slowest"])


def test_hash_batches_flagged():
    flags = {n["node"]: n["flags"] for n in report_for("pg_parallel_hash_psql.txt")["flagged"]}
    assert flags["Hash"] == ["hash split into 8 batches (spilled to disk)"]


def test_timing_off_still_reports_estimate_misses():
    report = report_for("pg_timing_off.json")
    assert report["has_timing"] is False
    assert report["slowest"] == []
    assert report["estimate_misses"][0]["factor"] == 500.0


def test_min_factor_and_top_are_configurable():
    report = report_for("pg_nested_loop_misestimate.json", top=1, min_factor=5000)
    assert names(report["estimate_misses"]) == ["Nested Loop (Inner)"]
    assert len(report["slowest"]) == 1


def test_never_executed_node_is_ignored_for_misses():
    plan = {"Plan": {"Node Type": "Seq Scan", "Relation Name": "t", "Plan Rows": 1000,
                     "Actual Total Time": 0.0, "Actual Rows": 0, "Actual Loops": 0}}
    assert report_module.analyze(plan)["estimate_misses"] == []


def test_index_only_scan_heap_fetches_flagged_when_large():
    node = {"Node Type": "Index Only Scan", "Actual Rows": 100, "Actual Loops": 1, "Heap Fetches": 80}
    assert report_module.flags(node) == ["80 heap fetches for 100 rows (visibility map not current, consider VACUUM)"]
    node["Heap Fetches"] = 2
    assert report_module.flags(node) == []


def test_lossy_bitmap_flagged():
    node = {"Node Type": "Bitmap Heap Scan", "Actual Rows": 10, "Actual Loops": 1, "Lossy Heap Blocks": 42}
    assert report_module.flags(node) == ["42 lossy heap blocks (bitmap exceeded work_mem)"]


def test_plain_explain_without_analyze_is_rejected():
    plan = [{"Plan": {"Node Type": "Seq Scan", "Relation Name": "t", "Plan Rows": 10}}]
    with pytest.raises(report_module.PlanError, match="ANALYZE"):
        report_module.analyze(report_module.load_plan(json.dumps(plan)))


@pytest.mark.parametrize("text", ["Seq Scan on t  (cost=0.00..1.00 rows=1 width=4)", "{}", "[]"])
def test_non_json_or_wrong_shape_is_rejected(text):
    with pytest.raises(report_module.PlanError):
        report_module.load_plan(text)


def test_cli_text_and_json(capsys):
    path = str(FIXTURES / "pg_nested_loop_misestimate.json")
    assert report_module.main([path]) == 0
    assert "1. Index Scan on orders o using orders_customer_id_idx: 2300.000 ms (95.8%)" in capsys.readouterr().out
    assert report_module.main([path, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["slowest"][0]["loops"] == 20000


def test_cli_error_exit_code(tmp_path, capsys):
    bad = tmp_path / "plan.txt"
    bad.write_text("Seq Scan on t")
    assert report_module.main([str(bad)]) == 2
    assert "FORMAT JSON" in capsys.readouterr().err


def test_cli_missing_file_exit_code(tmp_path, capsys):
    assert report_module.main([str(tmp_path / "missing.json")]) == 2
    assert "error:" in capsys.readouterr().err
