"""Zip the Cowork plugin (D-236) into dist/studysystem.plugin, ready to install in Cowork:

    uv run python scripts/build_plugin.py

Run it after every change under plugin/, then install the new file in Cowork.
"""

import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
PLUGIN = ROOT / "plugin"
DIST = ROOT / "dist"


def main() -> None:
    manifest = (PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    name = json.loads(manifest)["name"]
    DIST.mkdir(exist_ok=True)
    target = DIST / f"{name}.plugin"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(PLUGIN.rglob("*")):
            if path.is_file():
                # Forward slashes inside the zip, whatever the OS.
                archive.write(path, path.relative_to(PLUGIN).as_posix())
    print(target)


if __name__ == "__main__":
    main()
