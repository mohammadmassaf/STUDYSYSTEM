# STUDYSYSTEM

An MCP server that plans and tracks a student's semester: courses, past exams, topics, a ranked
study plan, logged hours. Python 3.13, official `mcp` SDK v2, managed with `uv`.

The server never calls a model. The host (Claude Desktop, Cowork...) does all the generation; the
server stores, validates and schedules. Generation is a two-tool contract: the host asks
`study_start_generation` for a task, does it, and hands the result to `study_submit_generation`,
which keeps what passes the schema.

## Tools

All tools start with `study_`. A failure comes back as `{code, message, field_errors[], fix}`.

- **Setup:** `study_add_course`, `study_set_course_input`, `study_add_assessment`,
  `study_set_assessment_input`, `study_set_capacity`
- **Evidence:** `study_add_past_exam`, `study_add_material`
- **Generation:** `study_start_generation`, `study_submit_generation` (transcription, notes,
  tagging), `study_confirm_transcription`, `study_export_notes`, `study_get_summary_task`, `study_save_summary`
- **Topics and profiles:** `study_get_topics`, `study_confirm_topic_proposal`,
  `study_derive_profile`
- **Plan and study loop:** `study_get_plan`, `study_start_session`, `study_submit_attempt`,
  `study_log_hours` (the only way minutes are recorded), `study_get_gate`
- **Smoke test:** `study_ping`

## Setup

1. Install [uv](https://docs.astral.sh/uv/), then from the repo: `uv sync`
2. `uv run study migrate` - builds the database and its one user. Run it again after every
   upgrade: the server refuses to start while the schema is behind.
3. Set `STUDYSYSTEM_VAULT_DIR` to the notes folder (e.g. an Obsidian vault's study folder).
   Each course's `vault_folder` sits under it, and its notes and summaries are written there as
   markdown and PDF. Without it notes are still saved in the database, but every export reports
   the missing setting.
4. Note PDFs are printed with a local Chrome, Chromium or Edge. If none is found on the usual
   paths, set `STUDYSYSTEM_CHROME` to the browser's executable.

### Connect an MCP host

The server speaks MCP on stdio. In the host's MCP config:

```json
{
  "mcpServers": {
    "studysystem": {
      "command": "uv",
      "args": ["--directory", "/path/to/STUDYSYSTEM", "run", "study", "serve"],
      "env": { "STUDYSYSTEM_VAULT_DIR": "/path/to/vault/study" }
    }
  }
}
```

## Where the data lives

- The database is `studysystem.db` in the platform data folder (on Windows
  `%LOCALAPPDATA%\studysystem`). Set `STUDYSYSTEM_DATA_DIR` to use another folder. Keep it out
  of cloud-synced folders.
- `snapshots/` next to it holds restore points: one copy per day at server start, plus one
  before every `study migrate` (`-premigrate-`) and `study downgrade` (`-predowngrade-`).
  To restore one, stop every server and copy it over `studysystem.db`.
- `papers/` holds the server's own copy of every past paper and material, named by its hash.

## Commands

- `uv run study migrate` - snapshot, then bring the schema to head.
- `uv run study serve` - start the MCP server on stdio.
- `uv run study downgrade <revision|base>` - undo migrations (development only; it drops
  tables and columns, so it snapshots first).

## Development

- `uv run pytest` - the full suite. Without a Postgres it proves SQLite only; the Postgres
  cases skip themselves.
- `uv run ruff check` - lint.
- To run both engines locally, bring up the Postgres in `compose.yaml`. The commands are in
  that file's header comment. CI runs both engines on every push.
