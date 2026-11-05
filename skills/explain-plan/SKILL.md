---
name: explain-plan
description: This skill should be used when the user pastes or attaches EXPLAIN or EXPLAIN ANALYZE output (PostgreSQL text or JSON, MySQL tabular, FORMAT=JSON, FORMAT=TREE or EXPLAIN ANALYZE), asks "why is this query slow", "read this query plan", "explain this EXPLAIN", "which index should I add", or mentions Seq Scan, Nested Loop, Hash Join, Sort Method external merge, rows vs actual rows, type ALL, Using filesort or Using temporary. Finds the bottleneck node, explains the cause and gives a concrete, verifiable fix.
version: 1.0.0
---

# Query Plan Explainer

Turn a pasted plan into: the bottleneck node, why it is slow, the concrete fix, and how to verify the fix.

## Step 1: identify the engine and format

| What you see | Engine and format | Has actual numbers? |
|---|---|---|
| `Seq Scan on t  (cost=0.00..431.00 rows=10000 width=244)` | PostgreSQL text | Only if `(actual time=... rows=... loops=...)` follows |
| `[{"Plan": {"Node Type": ...}}]` | PostgreSQL JSON | Only if `"Actual Total Time"` / `"Actual Rows"` keys exist |
| Table with `id, select_type, table, type, possible_keys, key, key_len, ref, rows, filtered, Extra` | MySQL traditional | No, estimates only |
| `{"query_block": {"select_id": 1, ...}}` | MySQL FORMAT=JSON (version 1) | No |
| Nested JSON objects with child iterators under `"inputs"` | MySQL FORMAT=JSON with `explain_json_format_version=2` (8.3+) | Only if `actual` keys are present |
| `-> Filter: ... (cost=... rows=...) (actual time=a..b rows=N loops=L)` | MySQL EXPLAIN ANALYZE (tree) | Yes |
| `-> Table scan on t (cost=... rows=...)` with no `actual` | MySQL FORMAT=TREE | No |

If the plan has only estimates, say so: the analysis is about what the planner expects, not what happened. Ask for an ANALYZE run when the question is "why was it slow", and remind the user that ANALYZE executes the statement (wrap data-changing statements in `BEGIN; ... ROLLBACK;` on PostgreSQL; on MySQL use a non-production copy, since EXPLAIN ANALYZE also runs multi-table UPDATE and DELETE).

Detailed node and field meanings, with official doc sources:
- PostgreSQL: `references/postgresql.md`
- MySQL: `references/mysql.md`
- Fix catalogue, index column order rules, framework syntax (Rails, Django, Laravel, Prisma): `references/fixes.md`

## Step 2: find the bottleneck

PostgreSQL ANALYZE:
1. `actual time` and `rows` are per loop averages. Node total time is `actual total time x loops`.
2. Times are inclusive of children. Exclusive time = node total minus the totals of its direct children. The bottleneck is the node with the largest exclusive time, not the top node.
3. Under `Gather` / `Gather Merge`, loops count the worker processes plus the leader, so divide by that number to approximate wall time.
4. Compare `rows` (estimate) with `actual rows` at every node. A miss of 10x or more is worth explaining; the first (deepest) node where it appears is usually the root cause, because errors multiply upward through joins. Ignore overestimates under a `Limit` (the node stopped early on purpose).
5. Then look at the bottleneck node for the specific signals in the table below.

If you can run Python and the plan is PostgreSQL JSON with ANALYZE, `scripts/pg_plan_report.py` (in the plugin root) computes exclusive times, the top 3 nodes and the worst estimate misses deterministically: `python3 scripts/pg_plan_report.py plan.json` (run from the plugin directory; `--json` for machine output, `--top N`, `--min-factor F`). Otherwise do the arithmetic by hand and show it.

MySQL traditional / JSON (estimates only): read rows top to bottom in join order. The product of `rows` down the join order gives the estimated rows examined. Look for `type` = `ALL` or `index` on large tables, `key` = NULL, `Using filesort`, `Using temporary`, `Using join buffer`.

MySQL EXPLAIN ANALYZE: same rules as PostgreSQL. `actual time=a..b` is in milliseconds, first row..all rows, averaged per loop and inclusive of children. Multiply by `loops`.

## Step 3: diagnose the cause

| Signal | Likely cause | Go to |
|---|---|---|
| PG `Seq Scan` with large `Rows Removed by Filter` relative to rows kept | No usable index for the filter, or the index is defeated | fixes.md "Missing or unusable index" |
| Filter on `lower(col)`, `col::date`, `date(col)`, `col + 0`, `COALESCE(col, ...)` | Function or cast on the column side defeats a plain index | fixes.md "Expression on the column" |
| `LIKE '%term'` or `ILIKE '%term%'` | Leading wildcard: B-tree cannot seek | fixes.md "Leading wildcard" |
| Comparing a string column to a number, or columns with different collations/charsets in a join (MySQL) | Implicit conversion on the column side | fixes.md "Type and collation mismatch" |
| `Nested Loop` whose inner side has huge `loops`, outer estimate far below actual | Row underestimate picked a nested loop; it now runs the inner side thousands of times | fixes.md "Bad row estimates" and "Nested loop explosion" |
| `Sort Method: external merge  Disk: NkB`, `Hash ... Batches: >1`, HashAggregate `Disk Usage`, temp blocks written | Operation exceeded `work_mem` (hash: `work_mem x hash_mem_multiplier`) | fixes.md "Spilling to disk" |
| Sort feeding a `Limit` | ORDER BY ... LIMIT without an index in that order | fixes.md "ORDER BY with LIMIT" |
| `Index Only Scan` with high `Heap Fetches` | Visibility map not current | fixes.md "Index only scan visiting the heap" |
| `Bitmap Heap Scan` with `lossy` heap blocks and big `Rows Removed by Index Recheck` | Bitmap exceeded `work_mem` | fixes.md "Spilling to disk" |
| Index Scan with a big `Filter` after `Index Cond` | Index matches only a prefix of the predicate | fixes.md "Composite index column order" |
| MySQL `type: ALL`, `key: NULL` | No usable index | fixes.md "Missing or unusable index" |
| MySQL `possible_keys` lists an index but `key` is NULL | Optimizer judged it not selective, or stats are stale, or expression/cast defeats it | fixes.md "Bad row estimates", "Expression on the column" |
| MySQL `key_len` shorter than the full composite index | Only a leftmost prefix was used; a range column stopped the rest | fixes.md "Composite index column order" |
| MySQL `Using filesort` | No index delivers rows in ORDER BY order | fixes.md "ORDER BY with LIMIT" |
| MySQL `Using temporary` | GROUP BY / DISTINCT / ORDER BY on different columns, or UNION | fixes.md "GROUP BY and temporary tables" |
| MySQL `Using join buffer (hash join)` or `(Block Nested Loop)` | No index on the join column of the inner table | fixes.md "Missing or unusable index" |
| Plan looks fine but query is slow | Time is outside the plan: planning time, triggers, network, locks, or the plan differs with real parameter values | Ask for `Planning Time`, `Triggers`, and a plan with the real bound values |

Never claim a cause the plan does not show. If you need the query text, table sizes, existing indexes, or `\d table` / `SHOW INDEX FROM table` output to be sure, ask for it, and say what you would conclude under each likely answer.

## Step 4: output format

Always answer in exactly this structure:

```
## Plan summary
Engine: PostgreSQL 16 / MySQL 8.x (guess if not stated, say so) | Format: ... | Actual numbers: yes/no
Total: <execution time if known> | Planning: <if known>

## Bottleneck
<node label> : <exclusive time and percent, or estimated cost share if no ANALYZE>
<one line showing the arithmetic, e.g. 0.115 ms x 20000 loops = 2300 ms of 2401 ms>

## Why
- <cause 1 tied to a specific field in the plan, quoted>
- <cause 2 if any>
Row estimate misses: <node: estimated X, actual Y (Zx under/over)> or "none above 10x"

## Fix
1. <primary fix as runnable SQL, e.g. CREATE INDEX CONCURRENTLY ... (col_a, col_b) ...>
   Why this column order: <one line>
2. <secondary fix or alternative, e.g. ANALYZE, CREATE STATISTICS, rewrite, work_mem>
Framework syntax: <only if the user mentioned Rails/Django/Laravel/Prisma or the project shows it>

## Verify
- Re-run: <exact EXPLAIN command>
- Expect: <the node type / field that should change, e.g. "Index Scan using ... with no Sort node", "Sort Method: quicksort Memory", "type: ref, Extra without Using filesort">
- Watch: <regression risks: write overhead of the new index, plan changes for other queries>
```

Keep it short. One primary fix, at most two alternatives. Give SQL the user can paste. Use `CREATE INDEX CONCURRENTLY` for PostgreSQL production tables and `ALGORITHM=INPLACE, LOCK=NONE` for MySQL, and note that CONCURRENTLY cannot run inside a transaction block.

## Safety

- Read only what the user pasted or pointed to. Do not open `.env`, credential files, `DATABASE_URL`, or anything under `~/.claude`.
- Do not run queries against a database. Give the user the commands to run.
- Suggest `SET enable_seqscan = off` (PostgreSQL) or `FORCE INDEX` (MySQL) only as session-level diagnostics to compare plans, never as the fix.
