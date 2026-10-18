# STUDYSYSTEM

An MCP server that plans and tracks a student's semester: courses, past exams, topics, a ranked
study plan, logged hours. The host model does all generation; this server stores, validates and
schedules.

## Commands

- Tests: `uv run pytest`
- Lint: `uv run ruff check`
- CI for a commit: `gh run list --commit $(git rev-parse HEAD)` - the full SHA; a short one
  matches nothing.
- Dependencies: `uv add <pkg>` (dev tools: `uv add --dev <pkg>`). uv owns the environment, so
  every Python command runs through `uv run`.

## Stack gotchas

- **mcp SDK v2:** the server class is `MCPServer` (`from mcp.server import MCPServer`). Most
  examples online, and most training data, show v1's `FastMCP` - translate them to v2.
- **SQLAlchemy Core** for table metadata and queries. The scheduler is hand-written SQL.
- **Alembic** runs with `render_as_batch=True`, because SQLite alters a table by rebuilding it.
- **Two engines, one test suite:** SQLite at runtime, Postgres in CI running the same tests.
  Every SQL construct runs on both - e.g. write an exactly-one check as a sum of
  `CASE WHEN x IS NULL THEN 0 ELSE 1 END` terms, the portable form.
- **Cowork cuts big replies silently:** past ~25,000 tokens of text, or ~760 KB of pictures.
  Generation tasks page under 50,000 chars / 600 KB (D-145, `services/parts.py`); a new tool
  that may return more must stay under it.

## Architecture rules

- **The server never calls a model.** Generation is a two-tool contract: `study_start_generation`
  hands the host a task, `study_submit_generation` validates what comes back (schema only).
- **Layers:** thin `tools/` -> `services/` (every rule and every write) -> `db/`. The dashboard's
  HTTP route calls the same services: one write entrance, two front doors.
- **Tools:** every name starts with `study_`. Errors are structured: `code`, message,
  `field_errors[]`, `fix`.
- **Write units:** a tool that changes more than one row runs inside the service layer's
  write-unit wrapper - one `BEGIN IMMEDIATE ... COMMIT`, and a failure rolls back the whole unit.
- **Unknown stays unknown:** a missing input is stored as `NULL` with tier `unknown`, never as 0.
- **Ownership:** every user-owned row carries `user_id`; the evidence tables carry `owner_id`.
- **Hours are explicit assertions.** Sessions label the work; minutes come only from
  `study_log_hours` (source `tool`) or the dashboard timer's stop (`timer`, D-198).
- **Migrations run only through the `study migrate` command.** A server whose database is behind
  head refuses to start with an error naming that command.
- **Rows retire by status** (`superseded`, `declined`, `voided_at`); foreign keys are
  `ON DELETE RESTRICT`.

## Git

- Work happens on a branch, never on `main`. One branch and one PR per milestone (a group of
  tasks in `build-plan.md`), one small commit per task. `main` changes only through a pull
  request with both CI jobs green, merged by rebase (the branch's commits land one by one, no
  merge commit). So each commit passes the tests on its own, and a PR is rebased onto the
  latest `main` before it can merge. Postgres runs only in CI, so no SQL reaches `main` unchecked.
- **Exception - workflow files:** a change that touches only `CLAUDE.md` or `.claude/` (agents,
  skills) commits straight to `main`: no branch, PR or QA run. Anything else follows the rules above.
- Small commits with an imperative subject ("add write-unit wrapper").
- Mohammad is the sole author of every commit: the message ends at its own body, with no
  `Co-Authored-By` or "generated with" trailer - and the same holds for PR titles and bodies.
  The repo is public.
