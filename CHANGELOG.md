# Changelog

A version that adds a migration (a change to the database's tables) begins with the line
**Run `study migrate`.** Until you run it, every tool and the dashboard refuse and name that
command. The README's "Upgrade" section has the whole upgrade as one line.

## 1.0.0

**Run `study migrate`** if you used the study system from a clone of the repo before. A new install
needs no migration: the first start builds the database.

The first release on PyPI.

- Install with `uv tool install studysystem`. `study setup` then asks for the output folder, connects
  Claude Desktop, and asks whether the dashboard starts at sign-in.
- The output folder is asked once and saved. A course with no declared folder gets one:
  `<semester>/<code> - <name>`.
- A server whose database is older than itself starts, and every tool except `study_dashboard`
  refuses until `study migrate` runs.
- `study stop` and `study start` stop and start the dashboard around an upgrade.
- The dashboard can start at sign-in on Windows and macOS (not yet tested on a Mac).
- The slash commands install from GitHub as a plugin.
