"""Walk every dashboard route at four widths and report console errors and horizontal overflow
(3.3, D-187). Run by hand against a database copy, never in CI:

    STUDYSYSTEM_DATA_DIR=<folder of a database copy> uv run python scripts/walk_dashboard.py

It starts the route itself, drives the installed Chrome, and exits 1 if any visit is not clean.
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"
WIDTHS = (1440, 1024, 768, 390)
HEIGHT = 900
PAGES = ("overview", "queue", "calendar", "courses", "grades", "load", "attention")


def wait_for_health() -> None:
    for _ in range(100):
        try:
            urllib.request.urlopen(f"{BASE}/health", timeout=1).close()
            return
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.2)
    sys.exit("the route did not answer /health")


def main() -> int:
    server = subprocess.Popen(
        [sys.executable, "-c", f"from studysystem.dashboard import main; main({PORT})"]
    )
    try:
        wait_for_health()
        with urllib.request.urlopen(f"{BASE}/state") as response:
            codes = [course["code"] for course in json.load(response)["courses"]]
        routes = [*PAGES, *(f"c/{code}" for code in codes)]

        problems: list[str] = []
        seen: list[str] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome")
            for width in WIDTHS:
                page = browser.new_page(viewport={"width": width, "height": HEIGHT})
                page.on("console", lambda m: seen.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e: seen.append(str(e)))
                page.on("requestfailed", lambda r: seen.append(f"request failed: {r.url}"))
                for route in routes:
                    seen.clear()
                    page.goto(f"{BASE}/#/{route}")
                    page.wait_for_selector("#view > *")
                    # The nav and cards are drawn after the fetch; give a late error time to show.
                    page.wait_for_timeout(150)
                    if "did not give a state" in page.inner_text("#view"):
                        problems.append(f"{width}px #/{route}: the page shows no state")
                    overflow = page.evaluate(
                        "const e = document.documentElement; e.scrollWidth - e.clientWidth"
                    )
                    label = f"{width}px #/{route}"
                    problems += [f"{label}: console: {text}" for text in seen]
                    if overflow > 0:
                        problems.append(f"{label}: scrolls sideways by {overflow}px")
                page.close()
            browser.close()

        visits = len(WIDTHS) * len(routes)
        print(f"{visits} visits ({len(routes)} routes x {len(WIDTHS)} widths)")
        for problem in problems:
            print(problem)
        print("clean" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    finally:
        server.terminate()


if __name__ == "__main__":
    sys.exit(main())
