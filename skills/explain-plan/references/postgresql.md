# PostgreSQL plan reference

Sources: PostgreSQL docs "Using EXPLAIN" (14.1), "EXPLAIN" command reference, "Index-Only Scans and Covering Indexes" (11.9), "Resource Consumption" (work_mem, hash_mem_multiplier), and the field names emitted by `src/backend/commands/explain.c`.

## Capturing a plan

```sql
EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT ...;   -- for scripts/pg_plan_report.py
EXPLAIN (ANALYZE, BUFFERS) SELECT ...;                -- readable text
EXPLAIN (ANALYZE, BUFFERS, SETTINGS) SELECT ...;      -- also shows non-default planner settings
```

- `ANALYZE` executes the statement. For INSERT, UPDATE, DELETE, MERGE, CREATE TABLE AS or EXECUTE, use `BEGIN; EXPLAIN ANALYZE ...; ROLLBACK;`.
- From PostgreSQL 18, `ANALYZE` turns on `BUFFERS` automatically (`BUFFERS OFF` disables it). Older versions need `BUFFERS` spelled out.
- `TIMING OFF` keeps row counts but drops per-node times (lower overhead). Then you can judge estimates but not where time went.
- `GENERIC_PLAN` (16+) explains a query with `$1` placeholders; it cannot be combined with `ANALYZE`.
- Prepared statements can switch to a generic plan after five executions; if the app is slow but a hand-typed EXPLAIN is fast, the app may be using a generic plan or different parameter values.
- In psql, `\pset format unaligned` (or `psql -XqAt -c "EXPLAIN (...) ..."`) avoid the `+` line wrapping. The script also strips psql decoration.

## Reading a node line

```
Index Scan using orders_customer_id_idx on orders o  (cost=0.43..8.50 rows=3 width=32) (actual time=0.010..0.115 rows=3 loops=20000)
```

| Part | Meaning |
|---|---|
| `cost=0.43..8.50` | Planner estimate in arbitrary cost units: startup cost (before first row) .. total cost (all rows). Inclusive of children. Not milliseconds. |
| `rows=3` (first parenthesis) | Estimated rows output per execution of the node |
| `width=32` | Estimated average row width in bytes |
| `actual time=0.010..0.115` | Milliseconds to first row .. to last row, average per loop, inclusive of children |
| `rows=3` (second parenthesis) | Actual rows output, average per loop. PostgreSQL 18 prints it with two decimals (`rows=0.50`) |
| `loops=20000` | Number of times the node was executed. Multiply time and rows by loops for totals |
| `never executed` | The node was never run (for example the other side of a join returned no rows) |

JSON keys: `Node Type`, `Relation Name`, `Alias`, `Index Name`, `Join Type`, `Startup Cost`, `Total Cost`, `Plan Rows`, `Plan Width`, `Actual Startup Time`, `Actual Total Time`, `Actual Rows`, `Actual Loops`, `Plans` (children), `Parent Relationship` (`Outer`, `Inner`, `InitPlan`, `SubPlan`, ...), `Subplan Name`, and at the top level `Planning Time`, `Execution Time`, `Triggers`.

## Exclusive time and the bottleneck

- Node total = `actual total time x loops`.
- Exclusive = node total minus the sum of its direct children's totals.
- Parallel plans: under `Gather` / `Gather Merge`, `loops` includes each worker plus the leader (unless `parallel_leader_participation` is off) and times are averaged across them. Dividing by `Workers Launched + 1` approximates wall time.
- CTE scans, InitPlans and SubPlans can make the subtraction imperfect (a CTE's work is charged to whichever node first pulls from it). Clamp negative exclusive times to zero and say the attribution is approximate.

## Row estimates

- Compare `rows` (estimate) to `actual rows`, both per loop. A 10x or larger miss is significant, 100x or more usually changes the join strategy.
- The deepest node with a big miss is usually the root cause; join estimates multiply upward.
- Overestimates under a `Limit`, or on the inner side of a semi/anti join, are expected: the node stopped early.
- Causes and fixes:
  - Stale statistics after bulk loads: `ANALYZE table;` (autovacuum may not have caught up).
  - Correlated columns (`country = 'DE' AND city = 'Berlin'`): the planner multiplies selectivities as if independent. Fix with `CREATE STATISTICS s (dependencies, mcv) ON country, city FROM customers; ANALYZE customers;` (`mcv` needs 12+).
  - Skewed values: raise the sample with `ALTER TABLE t ALTER COLUMN c SET STATISTICS 1000; ANALYZE t;` (default target is `default_statistics_target`, 100).
  - Expressions like `lower(email) = ...`: plain column stats do not apply. An expression index makes ANALYZE gather stats on the expression; `CREATE STATISTICS` on an expression (14+) does too without an index.
  - Functions in the predicate, `LIKE` patterns and parameters of unknown value get default selectivity guesses.

## Scan nodes

| Node | What it does | When it is a problem |
|---|---|---|
| `Seq Scan` | Reads every page of the table, applies `Filter`. `Rows Removed by Filter` is per loop. | Large table and the filter keeps a small fraction. Fine for small tables or when most rows match. |
| `Parallel Seq Scan` | Seq scan split across workers. | Same as Seq Scan; parallelism hides but does not fix it. |
| `Index Scan` | Walks the index for `Index Cond`, fetches each matching heap row, applies remaining `Filter`. Returns rows in index order. | Large `Rows Removed by Filter` means the index covers only part of the predicate. High loops under a Nested Loop. Many random heap reads (`Buffers: shared read`) on a low-selectivity condition. |
| `Index Only Scan` | Answers from the index alone. Still checks the visibility map per heap page; if the page is not all-visible it fetches the heap row (`Heap Fetches`, a total, not per loop). | `Heap Fetches` close to rows returned: no gain over Index Scan. `VACUUM` updates the visibility map. |
| `Bitmap Index Scan` + `Bitmap Heap Scan` | Builds a bitmap of matching pages from one or more indexes (`BitmapAnd` / `BitmapOr`), then reads the heap in physical order. `Recheck Cond` is re-evaluated on rows from lossy pages. `Heap Blocks: exact=N lossy=M`. | `lossy` blocks and big `Rows Removed by Index Recheck`: the bitmap did not fit in `work_mem` and degraded to page granularity. |
| `Tid Scan`, `Function Scan`, `Values Scan`, `CTE Scan`, `Subquery Scan` | Scan of TIDs, a set-returning function, a VALUES list, a CTE, a subquery. | CTE Scan with a filter applied after materialization: since 12 a non-recursive CTE referenced once is inlined unless `MATERIALIZED` is written. |

`Index Cond` means the condition is used to search the index. `Filter` means it is checked after the row is fetched. A condition you expected in `Index Cond` that shows up in `Filter` is the clue that the index is not usable for it.

## Join nodes

| Node | How it works | Warning signs |
|---|---|---|
| `Nested Loop` | For each outer row, runs the inner side once (inner `loops` = outer rows). Good when the outer side is small and the inner side is an index lookup. | Outer underestimated (planned 5, got 20000), so the inner side runs 20000 times. `Join Filter` with large `Rows Removed by Join Filter`: no index on the join condition. |
| `Hash Join` | Builds a hash table from the inner input (`Hash` node), probes it with the outer input. `Hash` shows `Buckets`, `Batches`, `Memory Usage`. | `Batches` above 1 means the hash table exceeded `work_mem x hash_mem_multiplier` and spilled to disk (`Original Hash Batches` shows what the planner expected). Hashing the larger side because of a bad estimate. |
| `Merge Join` | Merges two inputs sorted on the join key; inputs come from index scans or explicit `Sort` nodes. | Expensive `Sort` children, especially spilling ones. |
| `Memoize` (14+) | Caches inner-side results of a Nested Loop by parameter value. Shows `Hits`, `Misses`, `Evictions`, `Overflows`. | Few hits or many evictions: the cache is not helping (often from an ndistinct misestimate). |

## Sort, aggregate and other nodes

| Node | Fields | Warning signs |
|---|---|---|
| `Sort` | `Sort Key`, `Sort Method` (`quicksort`, `top-N heapsort`, `external sort`, `external merge`), `Memory: NkB` or `Disk: NkB` (JSON `Sort Space Used`, `Sort Space Type`) | `external merge  Disk: ...` means it spilled past `work_mem`. A Sort under a Limit means no index provides the ORDER BY. |
| `Incremental Sort` (13+) | `Presorted Key` | Usually good: input already partly ordered by an index. |
| `Aggregate`, `GroupAggregate`, `HashAggregate` | HashAggregate in 13+ shows `Batches`, `Memory Usage`, `Disk Usage` (JSON `HashAgg Batches`, `Disk Usage`) | `Disk Usage` or `Batches` above 1: spilled. |
| `Limit` | | Children may show fewer actual rows than estimated: normal. |
| `Gather`, `Gather Merge` | `Workers Planned`, `Workers Launched` | Launched below planned: `max_parallel_workers` or `max_worker_processes` exhausted. |
| `Materialize` | | Re-scanned many times under a Nested Loop. |
| `SubPlan`, `InitPlan` | | A correlated `SubPlan` with high loops: rewrite as a join or `LATERAL`. |

## Buffers

`Buffers: shared hit=36 read=6 dirtied=0 written=0 temp read=0 written=0`

- Counts are 8 kB blocks (default `block_size`), non-distinct, and include all child nodes.
- `shared hit`: found in PostgreSQL shared buffers. `shared read`: requested from the OS (may still come from the OS page cache). Many reads on the bottleneck node point at I/O.
- `dirtied` / `written`: pages this query modified, or had to write out to make room.
- `temp read/written`: spill files from sorts, hashes and materialization. Any temp written on a node means it exceeded `work_mem`.
- `local`: temporary tables.
- `I/O Timings` appear only with `track_io_timing = on`.

## Memory settings that show up in plans

- `work_mem` (default 4MB) is per sort or hash operation, not per query; several operations and sessions can each use it at once.
- Hash tables may use `work_mem x hash_mem_multiplier` (default 2.0 since 15; 1.0 in 13 and 14).
- Raise it for one query or transaction instead of globally: `SET LOCAL work_mem = '64MB';` inside a transaction, or `ALTER ROLE reporting SET work_mem = '64MB';` for a reporting role.
