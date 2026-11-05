# Fix catalogue

Each section: when it applies, the fix, and how to confirm. Index DDL uses the online form: PostgreSQL `CREATE INDEX CONCURRENTLY` (cannot run inside a transaction block, can leave an INVALID index if it fails, so check and drop it), MySQL `ALGORITHM=INPLACE, LOCK=NONE` (the statement errors out instead of locking if the engine cannot honour it).

## Missing or unusable index

Applies: PG `Seq Scan` with large `Rows Removed by Filter`; MySQL `type: ALL` or `Using join buffer`.

```sql
-- PostgreSQL
CREATE INDEX CONCURRENTLY idx_orders_customer_id ON orders (customer_id);
-- MySQL
ALTER TABLE orders ADD INDEX idx_orders_customer_id (customer_id), ALGORITHM=INPLACE, LOCK=NONE;
```

Skip it when the table is small or the filter keeps a large share of rows: a sequential scan is then correct. For a hot subset use a PostgreSQL partial index: `CREATE INDEX CONCURRENTLY ... (created_at) WHERE status = 'pending';`. Verify: the scan becomes `Index Scan` / `Bitmap Index Scan` (PG) or `type: ref` / `range` with `key` set (MySQL).

## Expression on the column

Applies: `WHERE lower(email) = ...`, `date(created_at) = ...`, `col + 0 = ...`, `COALESCE(col, x) = ...`.

1. Preferred: rewrite so the column stands alone. `created_at >= '2026-01-01' AND created_at < '2026-01-02'` instead of `date(created_at) = '2026-01-01'`.
2. Otherwise index the expression: PostgreSQL `CREATE INDEX CONCURRENTLY idx ON users (lower(email));` (query must use the same expression). MySQL 8.0.13+ `CREATE INDEX idx ON users ((LOWER(email)));`, or an indexed generated column on older versions.
3. MySQL with a case-insensitive collation: `LOWER()` is usually unnecessary, drop it.

## Leading wildcard

Applies: `LIKE '%term'`, `ILIKE '%term%'`. A B-tree can only seek on a fixed prefix.

- PostgreSQL: `CREATE EXTENSION pg_trgm;` then `CREATE INDEX CONCURRENTLY idx ON t USING gin (col gin_trgm_ops);` (GiST also works).
- Prefix searches (`LIKE 'abc%'`) in a non-C locale need `text_pattern_ops` on the index: `(col text_pattern_ops)`.
- MySQL: `FULLTEXT` index with `MATCH ... AGAINST` for word search. There is no trigram index; for suffix search store a reversed column, or use a search engine.

## Type and collation mismatch

Applies: MySQL comparing a string column to a number (`WHERE phone = 5551234` converts every row), joins between columns with different charsets or collations (for example `utf8mb3` against `utf8mb4`), PostgreSQL integer column compared with a `numeric` or `double precision` value, which can put a cast on the column side (look for `::` in `Filter`).

Fix the literal or parameter type first (`'5551234'`). For schema mismatch, align the column types: `ALTER TABLE t MODIFY col VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;` (a rebuild, plan it). Verify `key` is set and `ref` shows a column or `const`, not `func`.

## Bad row estimates

Applies: a 10x or larger gap between estimated and actual rows.

```sql
-- PostgreSQL
ANALYZE orders;                                           -- stale statistics
CREATE STATISTICS s_geo (dependencies, mcv) ON country, city FROM customers; ANALYZE customers;  -- correlated columns (mcv needs 12+)
ALTER TABLE orders ALTER COLUMN status SET STATISTICS 1000; ANALYZE orders;                       -- skewed values
-- MySQL
ANALYZE TABLE orders;
ANALYZE TABLE orders UPDATE HISTOGRAM ON status WITH 100 BUCKETS;   -- 8.0+
```

Verify: re-run EXPLAIN ANALYZE and compare estimated with actual at the node that was off. Fix the estimate before touching the join strategy.

## Nested loop explosion

Applies: PG `Nested Loop` with inner `loops` in the thousands; MySQL `loops=` large on the inner iterator.

1. Fix the outer estimate (section above), so the planner picks a hash or merge join on its own.
2. Make sure the inner join column is indexed so each loop is cheap: `CREATE INDEX CONCURRENTLY ... ON orders (customer_id);`.
3. A correlated `SubPlan` or `DEPENDENT SUBQUERY` with high loops: rewrite as a join, `LATERAL`, or `EXISTS`.
4. Diagnostic only, in a session: `SET enable_nestloop = off;` (PostgreSQL) to compare plans. Do not ship it.

Verify: the inner side's loops drop, or the join node becomes `Hash Join` / `Merge Join`, and total time falls.

## Spilling to disk

Applies: PG `Sort Method: external merge  Disk`, `Hash` with `Batches` above 1, HashAggregate `Disk Usage`, lossy bitmap blocks, `Temp Written Blocks`.

1. Reduce the data first: filter earlier, select fewer columns, or add an index that provides the order.
2. Raise memory for the one query: `BEGIN; SET LOCAL work_mem = '64MB'; ...; COMMIT;`. Hash operations may use `work_mem x hash_mem_multiplier`. Do not raise it globally without sizing: it is per operation, per session.
3. MySQL: sorts use `sort_buffer_size` and temporary tables are limited by `tmp_table_size` and `max_heap_table_size`; raise per session only after confirming the plan needs the sort or temporary table at all.

Verify: `Sort Method: quicksort  Memory`, `Batches: 1`, no temp blocks written.

## ORDER BY with LIMIT

Applies: PG `Sort` under `Limit`; MySQL `Using filesort` with a small LIMIT.

Put equality filter columns first, then the sort column, in one index: `WHERE tenant_id = ? ORDER BY created_at DESC LIMIT 20` needs `(tenant_id, created_at)`. The index can be scanned backwards for DESC (both engines can scan an index backwards; MySQL 8.0 added true descending index columns). Mixed directions (`a ASC, b DESC`) need the index declared that way on both engines.

For deep pages replace `OFFSET` with keyset pagination: `WHERE (created_at, id) < (?, ?) ORDER BY created_at DESC, id DESC LIMIT 20` (row-value comparison in MySQL has weaker index use on older versions, so verify with EXPLAIN).

Verify: no `Sort` node (PG) or no `Using filesort` (MySQL), and the Limit stops early.

## Index only scan visiting the heap

Applies: PG `Index Only Scan` with `Heap Fetches` close to the rows returned. The visibility map is not current, so the scan reads the table anyway.

`VACUUM (ANALYZE) orders;` and check autovacuum settings for that table (a lower `autovacuum_vacuum_scale_factor` for large, frequently updated tables). To make more queries index-only, `INCLUDE` payload columns (11+): `CREATE INDEX CONCURRENTLY ... ON orders (customer_id) INCLUDE (total);`. Verify `Heap Fetches` near 0.

## Composite index column order

- Equality columns first, then one range or sort column. Columns after the first range condition are not used for seeking.
- Among equality columns, order by what other queries reuse as a prefix; selectivity matters less than prefix reuse.
- A leftmost prefix serves queries on `(a)` and `(a, b)` but not `(b)` alone, so a separate `(a)` index is redundant once `(a, b)` exists.
- MySQL: `key_len` tells how many parts were used. PostgreSQL: a condition on a later column that appears as `Filter` rather than `Index Cond` was not used for the seek.
- Every index costs write time and space; drop the redundant one in the same change.

## GROUP BY and temporary tables

Applies: MySQL `Using temporary`; PG `HashAggregate` or `Sort` + `GroupAggregate` over a large input.

- MySQL: make GROUP BY and ORDER BY use the same columns in the same order; index the GROUP BY columns (after equality filter columns) so a loose or tight index scan avoids the temporary table (`Using index for group-by`). Since 8.0 GROUP BY no longer sorts implicitly, so add ORDER BY only when you need the order.
- PostgreSQL: an index on the grouping columns can allow `GroupAggregate` without a Sort; spilling hash aggregates are handled under "Spilling to disk".
- Aggregate after filtering, and avoid `DISTINCT` used to hide a join that multiplies rows.

## Framework syntax

Rails:
```ruby
disable_ddl_transaction!                                    # PostgreSQL concurrent index
add_index :orders, [:tenant_id, :created_at], algorithm: :concurrently
add_index :users, "lower(email)", name: "index_users_on_lower_email", algorithm: :concurrently
add_index :orders, :customer_id, algorithm: :inplace        # MySQL online DDL
```

Django:
```python
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models
from django.db.models.functions import Lower

class Migration(migrations.Migration):
    atomic = False
    operations = [AddIndexConcurrently("user", models.Index(Lower("email"), name="user_lower_email_idx"))]
```
On MySQL use a plain `AddIndex`. Trigram indexes: `TrigramExtension()` operation, then `GinIndex` with `opclasses=["gin_trgm_ops"]`.

Laravel:
```php
public $withinTransaction = false;   // on the migration class, PostgreSQL concurrent index
Schema::table('orders', fn (Blueprint $t) => $t->index(['tenant_id', 'created_at']));
DB::statement('CREATE INDEX CONCURRENTLY idx_users_lower_email ON users (lower(email))');
```

Prisma: `@@index([tenantId, createdAt])` in `schema.prisma` for plain composite indexes. Expression, partial and concurrent indexes are not expressible in the schema: write them in a hand-edited SQL migration and check how your Prisma version wraps migration files in a transaction before using `CONCURRENTLY`.
