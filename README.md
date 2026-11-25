# STUDYSYSTEM

An MCP server that plans and tracks a student's semester: courses, past exams, topics, a ranked
study plan, logged hours. Python 3.13 or later, official `mcp` SDK v2, managed with `uv`.

The server never calls a model. The host (Claude Desktop, Cowork...) does all the generation; the
server stores, validates and schedules. Generation is a two-tool contract: the host asks
`study_start_generation` for a task, does it, and hands the result to `study_submit_generation`,
which keeps what passes the schema.

## Install

You need Windows or macOS, and [Claude Desktop](https://claude.com/download) (Cowork is part of it).
You do not need this repo, and you do not need Python: uv brings its own.

1. **Open a terminal outside Claude.** On Windows: Start menu, Windows PowerShell. On a Mac: Terminal.
   Do every step there. Windows keeps what is made inside Claude private to it: uv's Python download
   fails there, and step 4 refuses.
2. **Install [uv](https://docs.astral.sh/uv/getting-started/installation/)**, then close the terminal
   and open a new one, so that it finds `uv`.

   Windows:

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

   macOS:

   ```sh
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

3. **Install the study system:**

   ```sh
   uv tool install studysystem
   ```

   If uv warns that its folder is not on your PATH, run `uv tool update-shell` and open a new
   terminal.
4. **Set up this computer.** Create a folder for your notes first (an Obsidian vault's study folder
   works too). Start Claude once if you never did. Then run:

   ```sh
   study setup
   ```

   It asks for the full path of that folder, tells you how to quit Claude, connects Claude, and asks
   whether the dashboard starts every time you sign in.
5. **Check it.** Start Claude, open a new Cowork task, and write: *Call study_ping.* The answer is
   the current time.

Run `study setup` before you paste an entry into any host by hand. Setup makes the data folder; on
Windows, a server that Claude starts first makes one that only Claude sees.

If setup finds no Claude config, start Claude, quit it, and run `study setup` again.

### The slash commands

The step-by-step flows are slash commands in a plugin: `/study-plan`, `/study-day`, `/add-chapter`
and `/explain-chapter`. Each one names the tools to call and their order, so the hours land on the
plan's item.

In Cowork: Customize > Plugins > Add marketplace, enter `mohammadmassaf/STUDYSYSTEM`, then install
`studysystem` from it. In Claude Code:

```sh
claude plugin marketplace add mohammadmassaf/STUDYSYSTEM
claude plugin install studysystem@studysystem
```

### Other hosts

The server speaks MCP on stdio. At its end, `study setup` prints the entry to paste into the host's
MCP config (Claude Code, Gemini CLI). It holds the full path of `study`:

```json
{
  "mcpServers": {
    "studysystem": {
      "command": "/full/path/to/study",
      "args": ["serve"]
    }
  }
}
```

### What setup decides

- **The output folder.** Notes and summaries are written under it as markdown and PDF, in each
  course's `vault_folder`; a course with none gets `<semester>/<code> - <name>`. With no output
  folder, notes are still saved in the database, but every export reports it.
  `STUDYSYSTEM_VAULT_DIR` is used only while no folder is saved. To change the folder, run
  `study setup` again.
- **The start at sign-in.** On Windows and macOS (not yet tested on a Mac). Your computer's startup
  settings turn it off.
  Without it, `study start` starts the dashboard in the background.
- **PDFs** are printed with a local Chrome, Chromium or Edge. If none is found on the usual paths,
  set `STUDYSYSTEM_CHROME` to the browser's executable.

## Upgrade

1. Quit Claude. Closing its window is not enough: on Windows use the icon near the clock, then Quit;
   on a Mac press Cmd+Q.
2. In a terminal outside Claude, paste this line:

   ```sh
   study stop; uv tool upgrade studysystem; study migrate; study start
   ```

3. Start Claude. In Cowork, Customize > Plugins, choose "Check for updates" on the marketplace.

`study migrate` copies the database first, then updates it. After an upgrade that changes the
database, the tools and the dashboard refuse until you run it, and name this command.
[CHANGELOG.md](https://github.com/mohammadmassaf/STUDYSYSTEM/blob/main/CHANGELOG.md) says which
versions do.

## Try it without your own data

See a working dashboard on invented data. After the install, run `study demo` and open
http://127.0.0.1:8766. Without installing it, uv alone is enough:

```sh
uvx --from studysystem study demo
```

The data is shaped like a real semester: nine courses, past papers, topics, a ranked plan and a month
of logged hours, all dated relative to the day you run it. The command builds its own database in a
folder of its own (`studysystem-demo` beside the real data folder) and rebuilds it on every run. It
never opens, reads or changes your own database.

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
- **Generator trials:** `study_set_trials` (off by default; see below)
- **Smoke test:** `study_ping`

### Generator trials (optional)

`study_set_trials` (or the switch on the dashboard's Plan page) turns on discrimination trials.
With them on, every walkthrough of a past paper hides one generated question among the paper's
questions. After each answer the host asks "real or generated?", the server records the guess,
and then it shows where the question came from. The guesses test how real the generated
questions look. They are off by default, because they add a question to every walkthrough
answer: turn them on only if you want to judge the generator.

## Where the data lives

- The database is `studysystem.db` in the platform data folder (on Windows
  `%LOCALAPPDATA%\studysystem`). The first start of the server or the dashboard builds it. Set
  `STUDYSYSTEM_DATA_DIR` to use another folder (`study setup` refuses while it is set). Keep it out
  of cloud-synced folders.
- `snapshots/` next to it holds restore points: one copy per day at server start, plus one
  before every `study migrate` (`-premigrate-`) and `study downgrade` (`-predowngrade-`).
  To restore one, stop every server and the dashboard, move `studysystem.db`,
  `studysystem.db-wal` and `studysystem.db-shm` (those that are there) to another folder, then
  copy the snapshot back as `studysystem.db`: an old `-wal` file changes or damages the copy. If the database goes missing while a copy is here, the server
  refuses to start instead of building an empty one;
  to start over with an empty one, run `study migrate`.
- `papers/` holds the server's own copy of every past paper and material, named by its hash.

## Commands

- `study setup` - set up this computer: the output folder, Claude, the start at sign-in.
- `study start`, `study stop` - start and stop the dashboard in the background.
- `study dashboard` - serve the dashboard in this terminal.
- `study demo` - the dashboard on invented data.
- `study migrate` - snapshot, then bring the database up to date. Run it after every upgrade.
- `study serve` - start the MCP server on stdio. The host runs this, not you.
- `study downgrade <revision|base>` - undo migrations (development only; it drops tables and columns,
  so it snapshots first).

## Development

Work on the code from a clone:

- `uv sync` - install the dependencies. Every command then runs through uv: `uv run study demo`,
  `uv run study migrate`.
- Without `STUDYSYSTEM_DATA_DIR`, a clone uses the same data folder as the installed one. Set it to
  another folder to leave your own data alone.
- `uv run pytest` - the full suite. Without a Postgres it proves SQLite only; the Postgres
  cases skip themselves.
- `uv run ruff check` - lint.
- To run both engines locally, bring up the Postgres in `compose.yaml`. The commands are in
  that file's header comment. CI runs both engines on every pull request and on every push to
  `main`.
- `uv run python scripts/build_plugin.py` - build the plugin as a file, `dist/studysystem.plugin`, to
  try a change under `plugin/` before it is on `main`. Raise `version` in
  `plugin/.claude-plugin/plugin.json` only at a release.
