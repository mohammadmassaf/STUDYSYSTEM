"""Walk every dashboard route at four widths and report console errors and horizontal overflow
(3.3, D-187), then check the plan's lanes (3.4, D-190). Run by hand against a database copy,
never in CI:

    STUDYSYSTEM_DATA_DIR=<folder of a database copy> uv run python scripts/walk_dashboard.py

It starts the route itself, drives the installed Chrome, and exits 1 if any visit is not clean.
"""

import copy
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


def visit(page, route: str, label: str, seen: list[str]) -> list[str]:
    """Open one route and return what is wrong with it."""
    seen.clear()
    page.goto(f"{BASE}/#/{route}")
    page.wait_for_selector("#view > *")
    # The nav and cards are drawn after the fetch; give a late error time to show.
    page.wait_for_timeout(150)
    problems = [f"{label}: console: {text}" for text in seen]
    if "did not give a state" in page.inner_text("#view"):
        problems.append(f"{label}: the page shows no state")
    overflow = page.evaluate("const e = document.documentElement; e.scrollWidth - e.clientWidth")
    if overflow > 0:
        problems.append(f"{label}: scrolls sideways by {overflow}px")
    return problems


def new_page(browser, width: int, seen: list[str]):
    page = browser.new_page(viewport={"width": width, "height": HEIGHT})
    page.on("console", lambda m: seen.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: seen.append(str(e)))
    page.on("requestfailed", lambda r: seen.append(f"request failed: {r.url}"))
    return page


def lane_codes(page, lane: str) -> list[str]:
    return page.eval_on_selector_all(
        f'[data-lane="{lane}"] .plan-row', "rows => rows.map(r => r.dataset.code)"
    )


def check_lanes(page, plan: dict) -> list[str]:
    """3.4's Done-when (D-190): every shown item sits in its own lane's card, and a course with
    a first-material item - nothing taught yet - never shows under Study."""
    lanes = plan["lanes"]
    page.goto(f"{BASE}/#/overview")
    page.wait_for_selector("#view > *")
    problems = []
    drawn = {lane: lane_codes(page, lane) for lane in lanes}
    for lane in lanes:
        sent = [item["code"] for item in lanes[lane]["items"]]
        print(f"{lane}: {len(drawn[lane])} drawn {drawn[lane]}")
        if drawn[lane] != sent:
            problems.append(f"lane {lane}: /state sends {sent}, the page draws {drawn[lane]}")
    cold = sorted(
        {item["code"] for item in lanes["setup"]["items"] if item["setup_kind"] == "first-material"}
    )
    print(f"first-material courses: {cold}")
    problems += [
        f"{code} has nothing taught but shows under Study"
        for code in cold
        if code in drawn["study"]
    ]
    return problems


def with_a_review(state: dict) -> dict | None:
    """The stored plan with one review item, built from its first study item: no review rows
    exist yet, and this draws the lane without writing to any database."""
    lanes = state["plan"]["current"]["lanes"]
    if not lanes["study"]["items"]:
        return None
    study = lanes["study"]["items"][0]
    review = {
        key: study[key]
        for key in ("course_id", "code", "reason", "topic_id", "topic_name", "score")
    }
    fake = copy.deepcopy(state)
    fake["plan"]["current"]["lanes"]["review"]["items"] = [{"rank": 1, **review}]
    return fake


def main() -> int:
    server = subprocess.Popen(
        [sys.executable, "-c", f"from studysystem.dashboard import main; main({PORT})"]
    )
    try:
        wait_for_health()
        with urllib.request.urlopen(f"{BASE}/state") as response:
            state = json.load(response)
        codes = [course["code"] for course in state["courses"]]
        routes = [*PAGES, *(f"c/{code}" for code in codes)]

        problems: list[str] = []
        seen: list[str] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome")
            for width in WIDTHS:
                page = new_page(browser, width, seen)
                for route in routes:
                    problems += visit(page, route, f"{width}px #/{route}", seen)
                page.close()

            plan = state["plan"]["current"]
            fake = None
            if plan is None:
                problems.append("no stored plan: the lanes cannot be checked")
            else:
                page = new_page(browser, WIDTHS[0], seen)
                problems += check_lanes(page, plan)
                page.close()
                fake = with_a_review(state)
                if fake is None:
                    print("no study item to build a review from: the review lane is not drawn")

            for width in WIDTHS if fake else ():
                page = new_page(browser, width, seen)
                page.route(f"{BASE}/state", lambda request: request.fulfill(json=fake))
                for route in ("overview", "queue"):
                    label = f"{width}px #/{route} with a review"
                    problems += visit(page, route, label, seen)
                    if not page.query_selector('[data-lane="review"] [data-uid^="review:"]'):
                        problems.append(f"{label}: the review item is not drawn")
                page.close()
            browser.close()

        visits = len(WIDTHS) * len(routes)
        print(f"{visits} visits ({len(routes)} routes x {len(WIDTHS)} widths)")
        if fake:
            print(f"{len(WIDTHS) * 2} visits with a review item (overview, queue)")
        for problem in problems:
            print(problem)
        print("clean" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    finally:
        server.terminate()


if __name__ == "__main__":
    sys.exit(main())
