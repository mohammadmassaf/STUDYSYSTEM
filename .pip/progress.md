# PIP Progress — STUDYSYSTEM (M1)

_Source: build-plan.md, M1 only (tracer bullet). Planned 2026-09-18. Parts are M1's five blocks. 15 new concepts + 3 reused (database-migrations, custom-exception-handlers, structured-llm-output)._

## Concept tree

### Plumbing (1.1-1.6)
- [ ] **SQLite concurrency model: WAL, busy_timeout, BEGIN IMMEDIATE** — `new` · prereqs: none · code: `src/studysystem/db/engine.py:28`
- [ ] **Database migrations with Alembic** — `known` (Mealwise ✓, ungraded) · prereqs: sqlalchemy-orm · code: `src/studysystem/db/migrate.py:46 (check_schema) + migrations/env.py:21 (render_as_batch)`
- [ ] **Schema invariants in DDL: CHECK, FK RESTRICT, ownership columns** — `new` · prereqs: database-migrations · code: `1.4 migration 001 (planned)`
- [ ] **Cross-engine SQL portability (one suite, SQLite + Postgres)** — `new` · prereqs: schema-invariants-in-ddl · code: `1.5 CI matrix (planned)`
- [ ] **Transactional write units and the single write entrance** — `new` · prereqs: sqlite-concurrency-model · code: `1.6 services write-unit wrapper (planned)`
- [ ] **Time-sortable identifiers (ULID) vs autoincrement / uuid4** — `new` · prereqs: none · code: `1.6 (planned)`
- [ ] **Custom HTTP exception handlers** — `known` (Mealwise ✓, ungraded) · prereqs: fastapi-app-lifecycle · code: `src/studysystem/db/migrate.py:15 (SchemaBehindHead: code/message/fix)`

### Intake (1.7-1.8)
- [ ] **MCP tool contract: host, server, tool schema, thin tool over service** — `new` · prereqs: none · code: `src/studysystem/tools/ping.py + 1.7 (planned)`
- [ ] **Explicit unknowns: NULL + tier unknown, never zero** — `new` · prereqs: schema-invariants-in-ddl · code: `1.7 study_set_course_input (planned)`
- [ ] **By-value intake across the host/server boundary (content, never paths)** — `new` · prereqs: mcp-tool-contract · code: `1.8 file intake layer (planned)`

### Contract (1.9-1.11)
- [ ] **Structured LLM output with schema validation** — `known` (GF 7/10) · prereqs: prompt-engineering · code: `1.10 schema-only validation (planned)`
- [ ] **Generation contract: the server never calls a model** — `new` · prereqs: mcp-tool-contract, structured-llm-output · code: `1.9 study_start_generation / 1.10 study_submit_generation (planned)`
- [ ] **Idempotent submission with item-level partial acceptance** — `new` · prereqs: generation-contract, transactional-write-units · code: `1.10 unique (task_id, item_ordinal) + 3-cap (planned)`
- [ ] **Ports and adapters: one core, interchangeable entrances** — `new` · prereqs: generation-contract · code: `1.11 route-interchangeability test (planned)`

### Derivation and content (1.12-1.14)
- [ ] **Status-based row lifecycle: retire by status, never delete** — `new` · prereqs: schema-invariants-in-ddl · code: `1.12 study_confirm_topic_proposal (planned)`
- [ ] **Recency-weighted evidence aggregation** — `new` · prereqs: status-based-lifecycle · code: `1.13 exam_profile derivation (planned)`

### The loop (1.15-1.17)
- [ ] **Priority-scoring scheduler: weight x weakness x proximity / hours** — `new` · prereqs: recency-weighted-aggregation, cross-engine-sql-portability · code: `1.15 study_get_plan SQL (planned)`
- [ ] **Append-only event ledger: explicit assertions, void never edit** — `new` · prereqs: status-based-lifecycle, transactional-write-units · code: `1.16 study_log_hours (planned)`

## Mastery list (deduped — known across all projects)
- (none yet in this project)

## Dropped prerequisite edges (log)
- (none — parts ordered so every prereq sits in the same or an earlier part)
