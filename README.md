# query-plan-explainer

A plugin that reads `EXPLAIN` / `EXPLAIN ANALYZE` output from PostgreSQL or MySQL and tells you which node is slow, why, what to change and how to confirm it worked.

It pairs with [db-migration-guard](https://github.com/a-abdellatif98/db-migration-guard) and [backfill-planner](https://github.com/a-abdellatif98/backfill-planner): the fix for a slow plan is often an index, and db-migration-guard reviews that migration.

## What you get

- **Bottleneck by exclusive time**: node total (`actual time x loops`) minus its children, not the top node
- **Estimate misses**: the deepest node where estimated and actual rows differ by 10x or more
- **Causes tied to plan fields**: seq scans with big `Rows Removed by Filter`, nested loop explosions, disk sorts and hash spills, lossy bitmaps, index only scans visiting the heap, `type: ALL`, `Using filesort`, `Using temporary`, join buffers
- **Defeated indexes**: functions, casts, leading wildcards, type and collation mismatches
- **Runnable fixes**: online index DDL for each engine, plus Rails, Django, Laravel and Prisma syntax
- **A verify step**: the exact `EXPLAIN` to re-run and what should change

## Installation

```
/plugin install query-plan-explainer@claude-plugins-official
```

## Usage

### Slash command

```
/explain-plan plan.json
/explain-plan
```

Pass a file path, or run it with no argument and paste the plan in the conversation. The command reads files and models with Read, Glob and Grep only, to learn the framework and existing indexes.

### Skill

The `explain-plan` skill activates when you paste a plan or ask why a query is slow. References:

| Reference | Covers |
|---|---|
| `postgresql.md` | Capturing plans, reading node lines, exclusive time, parallel plans, row estimates, scan, join, sort and aggregate nodes, buffers, `work_mem` |
| `mysql.md` | Traditional, JSON and tree formats, `type` and `Extra` values, `EXPLAIN ANALYZE`, statistics, histograms, invisible indexes |
| `fixes.md` | Fix catalogue by cause, composite index column order, framework syntax |

### Plan report script

For a PostgreSQL plan captured with `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`:

```
python3 scripts/pg_plan_report.py plan.json
python3 scripts/pg_plan_report.py plan.json --json --top 5 --min-factor 20
```

Prints the slowest nodes by exclusive time, the worst row estimate misses and warnings such as disk spills. It handles parallel plans, `Limit` and `TIMING OFF`, and accepts psql-wrapped output. It does not parse MySQL plans or PostgreSQL text plans.

## Safety

The plugin ships no hooks. The command only has Read, Glob and Grep. Nothing reads `.env`, database URLs or other credentials, nothing connects to a database, and nothing runs a query: you run the `EXPLAIN` yourself and paste the result. Remember that `EXPLAIN ANALYZE` executes the statement, so wrap data-changing statements in a transaction you roll back, or use a non-production copy.

## Development

```
pip install pytest pyflakes
python3 -m pytest -q tests
python3 -m pyflakes scripts tests
```

## License

MIT
