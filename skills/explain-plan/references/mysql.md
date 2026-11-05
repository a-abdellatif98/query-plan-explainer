# MySQL plan reference

Sources: MySQL Reference Manual "EXPLAIN Output Format", "Extended EXPLAIN Output Format", "Obtaining Execution Plan Information", "Optimizing Queries with EXPLAIN", "Optimization and Indexes", "ORDER BY Optimization", "GROUP BY Optimization". Version notes below are the ones I am confident about; check the manual for your exact release (MariaDB differs: its `ANALYZE` statement replaces `EXPLAIN ANALYZE` and the output is not the tree format).

## Capturing a plan

```sql
EXPLAIN SELECT ...;                      -- traditional table, estimates only
EXPLAIN FORMAT=JSON SELECT ...;          -- estimates, more detail (cost, used_key_parts, attached_condition)
EXPLAIN FORMAT=TREE SELECT ...;          -- iterator tree, estimates only (8.0.16+)
EXPLAIN ANALYZE SELECT ...;              -- runs the query, tree with actual numbers (8.0.18+)
SHOW WARNINGS;                           -- after EXPLAIN: the rewritten query the optimizer sees
```

- `EXPLAIN ANALYZE` executes the statement. Use a non-production copy for anything that writes (it also executes multi-table UPDATE and DELETE).
- `EXPLAIN ANALYZE` output is always the tree format.
- From 8.3, the `explain_json_format_version` system variable can select a newer JSON layout built from iterators. If the JSON has nested objects with an `inputs` list instead of `query_block`, treat it like the tree format.

## Traditional columns

| Column | Meaning | Look for |
|---|---|---|
| `select_type` | `SIMPLE`, `PRIMARY`, `SUBQUERY`, `DEPENDENT SUBQUERY`, `DERIVED`, `UNION`, `MATERIALIZED` | `DEPENDENT SUBQUERY` runs once per outer row: rewrite as a join. |
| `table` | Table or alias, or `<derived2>`, `<union1,2>` | Derived tables that are materialized and then scanned. |
| `type` | Join type, best to worst: `system`, `const`, `eq_ref`, `ref`, `fulltext`, `ref_or_null`, `index_merge`, `unique_subquery`, `index_subquery`, `range`, `index`, `ALL` | `ALL` is a full table scan. `index` is a full index scan, cheaper than `ALL` only when the index is narrower than the row. |
| `possible_keys` | Indexes that could apply | Listed but unused (`key` NULL) means the optimizer rejected it. |
| `key` | Index actually chosen | NULL means no index. |
| `key_len` | Bytes of the index used | Shorter than the full index means only a leftmost prefix is used. |
| `ref` | What is compared to the index (`const`, a column, `func`) | `func` means an expression on the other side. |
| `rows` | Estimated rows to examine in this step | Multiply down the join order for total estimated work. |
| `filtered` | Estimated percent of examined rows that pass the table condition | Low `filtered` with high `rows` means the index is not selective for the full predicate. |
| `Extra` | Notes, see below | |

Rough work estimate for a join: `rows(step1) x rows(step2) x ...`, each step's `rows` already being per outer row. It is an estimate, not a measurement.

## Extra values worth acting on

| Extra | Meaning | Action |
|---|---|---|
| `Using filesort` | Rows are sorted in a separate pass (memory or disk). Not necessarily a file. | Index in ORDER BY order (see fixes.md "ORDER BY with LIMIT"). |
| `Using temporary` | An internal temporary table is created, for GROUP BY, DISTINCT, ORDER BY over a different column set, or UNION. | fixes.md "GROUP BY and temporary tables". |
| `Using where` | Rows are filtered by the server after the storage engine returns them. | Normal. Combined with `type: ALL` it means the filter is not supported by an index. |
| `Using index` | Covering index: the query is answered from the index alone. | Good. |
| `Using index condition` | Index condition pushdown: the engine tests part of the condition on the index entry before reading the row. | Good. |
| `Using join buffer (hash join)` | No usable index on the join column; a hash join is used (8.0.18+). | Index the inner table's join column. |
| `Using join buffer (Block Nested Loop)` | Same situation on older versions (block nested loop was replaced by hash join in 8.0.20). | Same. |
| `Using index for group-by` | Loose index scan for GROUP BY / DISTINCT. | Good. |
| `Using MRR` | Multi-range read: row lookups sorted by primary key to reduce random IO. | Informational. |
| `Impossible WHERE`, `no matching row in const table` | The optimizer proved the result empty. | Check the predicate. |
| `Select tables optimized away` | Answered from metadata (for example `MIN`/`MAX` on an indexed column). | Good. |

## EXPLAIN ANALYZE tree format

```
-> Nested loop inner join  (cost=1.2e+6 rows=19800) (actual time=0.9..2310 rows=20000 loops=1)
    -> Table scan on c  (cost=100 rows=1000) (actual time=0.1..12.4 rows=1000 loops=1)
    -> Index lookup on o using idx_customer (customer_id=c.id)  (cost=3.1 rows=20) (actual time=0.008..2.2 rows=20 loops=1000)
```

| Part | Meaning |
|---|---|
| `cost=` | Optimizer cost units, not time. |
| `rows=` (first group) | Estimated rows per execution. |
| `actual time=a..b` | Milliseconds to first row .. to all rows, average per loop, inclusive of children. |
| `rows=` (second group) | Actual rows per loop. |
| `loops=` | Executions of the iterator. Total time is `b x loops`. |
| `Filter:`, `Index lookup`, `Index range scan`, `Table scan`, `Covering index scan`, `Sort`, `Aggregate using temporary table`, `Stream results`, `Materialize` | Iterator names. A `Filter` directly above a `Table scan` is a full scan with a post-filter. |

Same method as PostgreSQL: exclusive time = node total minus its children's totals, compare estimated and actual rows, start from the deepest large miss. `scripts/pg_plan_report.py` does not parse this format, so do the arithmetic by hand and show it.

## Statistics and optimizer helpers

- `ANALYZE TABLE t;` refreshes index statistics. InnoDB estimates cardinality by sampling pages (`innodb_stats_persistent_sample_pages`).
- Histograms (8.0+): `ANALYZE TABLE t UPDATE HISTOGRAM ON col WITH 100 BUCKETS;` improve estimates for non-indexed columns used in filters and joins. They do not replace indexes.
- `SHOW INDEX FROM t;` shows column order and cardinality.
- Invisible indexes (8.0+): `ALTER TABLE t ALTER INDEX idx INVISIBLE;` tests dropping an index safely.
- Index hints `USE INDEX`, `FORCE INDEX`, `IGNORE INDEX` are for diagnosing a plan comparison, not for shipping.
- Functional key parts (8.0.13+): `CREATE INDEX idx ON users ((LOWER(email)));` indexes an expression. Generated columns with an index are the alternative on older versions.
- Descending index columns are honoured from 8.0; before that the `DESC` keyword was parsed and ignored.
