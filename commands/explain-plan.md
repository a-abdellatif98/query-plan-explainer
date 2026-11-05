---
description: Explain a pasted EXPLAIN or EXPLAIN ANALYZE plan (PostgreSQL or MySQL): bottleneck, cause, concrete fix and how to verify it
argument-hint: [path to plan file, or paste the plan]
allowed-tools: [Read, Glob, Grep]
---

# Explain a Query Plan

Analyse a query plan following the `explain-plan` skill.

## Arguments

The user invoked this command with: $ARGUMENTS

Treat the argument as a path to a file containing a plan, or as the plan text itself. If it is empty, look at the conversation for a pasted plan; if there is none, ask the user to run `EXPLAIN (ANALYZE, BUFFERS) <query>` (PostgreSQL) or `EXPLAIN ANALYZE <query>` (MySQL 8.0.18+) and paste the output.

## Instructions

1. **Get the plan.** If the argument is a path, Read that file only. Never open `.env`, `.env.*`, `config/database.yml`, `settings.py` `DATABASES`, `config/database.php`, credential files, or anything holding tokens or database URLs. Do not connect to a database or run queries: give the user the commands to run.

2. **Identify the engine and format** with the table in the skill (PostgreSQL text or JSON, MySQL traditional, JSON, tree). State whether the plan has actual numbers.

3. **Find the bottleneck** as described in the skill: exclusive time per node and estimate versus actual rows for ANALYZE plans, `type` / `key` / `Extra` for estimate-only MySQL plans. Show the arithmetic.

4. **Optionally read the project** for context only: Grep for the table names in models, `db/schema.rb`, `db/structure.sql` or migrations to learn existing indexes and the framework (Rails, Django, Laravel, Prisma), so the fix uses the right syntax and does not duplicate an index.

5. **Diagnose and fix** using `skills/explain-plan/references/` (`postgresql.md`, `mysql.md`, `fixes.md`). If the query text, table sizes or existing indexes are needed to be sure, ask for them and say what you would conclude under each likely answer.

## Output format

Use the exact structure from Step 4 of the skill: Plan summary, Bottleneck, Why, Fix, Verify. One primary fix, at most two alternatives, SQL the user can paste.
