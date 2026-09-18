# STUDYSYSTEM

An MCP server for studying. Python 3.13, official `mcp` SDK v2, managed with `uv`.

Currently: a smoke-test server with one tool, `ping`, that returns the server time.

## Running

- `uv run study migrate` — bring the database schema to head. Run it once before the first
  start and again after every upgrade; the server refuses to start while the schema is behind.
- `uv run study serve` — start the MCP server on stdio.
- `uv run study downgrade base` — undo every migration (development only).
