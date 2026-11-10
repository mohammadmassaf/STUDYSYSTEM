"""Walk every dashboard page at four widths and report console errors and horizontal overflow
(3.3, D-187), then check the four pages of D-260: the Plan's lanes against /state (D-190, D-238),
a setup task with no Start button, the Today list's collapse arrow, the theme switch, a review
item, and the Schedule's blocks. Run by hand against a database copy, never in CI:

    STUDYSYSTEM_DATA_DIR=<folder of a database copy> uv run python scripts/walk_dashboard.py

It starts the route itself, drives the installed Chrome, and exits 1 if any visit is not clean.
Nothing it does writes to the database: the faked states are served to the page only.
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
PAGES = ("today", "plan", "schedule", "courses")
LANE_NAMES = {"setup": "Setup", "study": "Study", "review": "Review"}


def wait_for_health() -> None:
    for _ in range(100):
        try:
            urllib.request.urlopen(f"{BASE}/health", timeout=1).close()
            return
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.2)
    sys.exit("the route did not answer /health")


def open_page(page, route: str) -> None:
    page.goto(f"{BASE}/#/{route}")
    page.wait_for_selector("#app > *")
    # The page draws after the fetch; give a late error time to show.
    page.wait_for_timeout(150)


def visit(page, route: str, label: str, seen: list[str]) -> list[str]:
    """Open one page and return what is wrong with it."""
    seen.clear()
    open_page(page, route)
    problems = [f"{label}: console: {text}" for text in seen]
    if "Check the study server" in page.inner_text("#app"):
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


def sent_codes(lanes: dict) -> dict[str, list[str]]:
    """The course code of every task /state sends, per lane, as the page lists it: the study lane
    as its chapter entries, plus any item no entry covers (D-238)."""
    out = {}
    for lane, body in lanes.items():
        rows = list(body.get("entries") or [])
        covered = {t for entry in rows for t in entry.get("topic_ids") or []}
        rows += [item for item in body.get("items") or [] if item.get("topic_id") not in covered]
        out[lane] = sorted(row["code"] for row in rows)
    return out


def drawn_codes(page) -> dict[str, list[str]]:
    """The course code of every task the Plan page draws, per lane section."""
    pairs = page.eval_on_selector_all(
        ".plan-lane .pitem",
        "items => items.map(i => [i.closest('.plan-lane').querySelector('h2').innerText,"
        " i.closest('.cgroup').querySelector('header b').innerText])",
    )
    out: dict[str, list[str]] = {name: [] for name in LANE_NAMES.values()}
    for lane, code in pairs:
        out.setdefault(lane, []).append(code)
    return {lane: sorted(codes) for lane, codes in out.items()}


def check_lanes(page, plan: dict) -> list[str]:
    """3.4's Done-when (D-190) on the D-260 Plan page: every task /state sends sits in its own
    lane's section, once, and a course with a first-material item - nothing taught yet - never
    shows under Study."""
    open_page(page, "plan")
    sent, drawn = sent_codes(plan["lanes"]), drawn_codes(page)
    problems = []
    for lane, name in LANE_NAMES.items():
        print(f"{lane}: {len(drawn.get(name, []))} drawn {drawn.get(name, [])}")
        if drawn.get(name, []) != sent.get(lane, []):
            problems.append(
                f"lane {lane}: /state sends {sent.get(lane)}, the page draws {drawn.get(name)}"
            )
    cold = sorted(
        {
            item["code"]
            for item in plan["lanes"]["setup"]["items"]
            if item.get("setup_kind") == "first-material"
        }
    )
    print(f"first-material courses: {cold}")
    problems += [
        f"{code} has nothing taught but shows under Study"
        for code in cold
        if code in drawn.get("Study", [])
    ]
    return problems


def check_today(page, plan: dict | None) -> list[str]:
    """D-260 on Today: a setup task has no Start button (it is never timed), every other task
    has one, and the arrow collapses the list to the next task and opens it again. The list must
    hold a setup row and a timed row whenever /state sends one, so neither check passes empty."""
    open_page(page, "today")
    problems = []
    rows = page.eval_on_selector_all(
        ".timeline .entry",
        "es => es.map(e => [e.querySelector('.eyebrow').innerText.split(' · ')[0],"
        " !!e.querySelector('[data-act=start]')])",
    )
    sent = sent_codes(plan["lanes"]) if plan else {}
    drawn_lanes = {lane for lane, _ in rows}
    if sent.get("setup") and "Setup" not in drawn_lanes:
        problems.append("today: /state sends setup tasks but Today lists none")
    if (sent.get("study") or sent.get("review")) and not drawn_lanes - {"Setup"}:
        problems.append("today: /state sends study or review tasks but Today lists none")
    for lane, has_start in rows:
        if lane == "Setup" and has_start:
            problems.append("today: a setup task has a Start button")
        if lane != "Setup" and not has_start:
            problems.append(f"today: a {lane} task has no Start button")
    if page.query_selector(".focusbox h2") and page.evaluate("focus() && focus().lane === 'setup'"):
        problems.append("today: the next study block is a setup task")
    if len(rows) > 1:
        page.click(".chev")
        if (shown := len(page.query_selector_all(".timeline .entry"))) != 1:
            problems.append(f"today: collapsed, the list shows {shown} tasks, not 1")
        page.click(".chev")
        if (shown := len(page.query_selector_all(".timeline .entry"))) != len(rows):
            problems.append(f"today: opened again, the list shows {shown} of {len(rows)} tasks")
    else:
        print(f"today: {len(rows)} task(s) listed, so the collapse arrow is not checked")
    return problems


def check_theme(page) -> list[str]:
    """D-260: the sun and moon switch sets the theme and marks the one in use."""
    open_page(page, "today")
    problems = []
    for name in ("light", "dark"):
        page.click(f'.theme-switch [data-id="{name}"]')
        theme = page.evaluate("document.documentElement.dataset.theme")
        pressed = page.get_attribute(f'.theme-switch [data-id="{name}"]', "aria-pressed")
        if theme != name or pressed != "true":
            problems.append(f"theme: pressing {name} gave theme {theme}, pressed {pressed}")
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


def with_a_timetable(state: dict) -> dict | None:
    """The stored state with a fixed week of classes, so the Schedule page's blocks can be
    counted whatever the copy holds. Two meetings overlap and one is inferred. Every one falls
    Monday to Friday, the days the D-260 grid draws."""
    courses = state["courses"]
    if not courses:
        return None
    fake = copy.deepcopy(state)
    spec = [
        (1, "08:00", "09:40", 0, "lecture", "declared"),
        (1, "08:00", "09:40", 1, "lab", "declared"),
        (1, "12:00", "13:40", 2, "lecture", "declared"),
        (2, "09:55", "11:35", 1, "lecture", "inferred"),
        (3, "14:00", "15:40", 0, "lab", "declared"),
        (5, "16:00", "17:40", 2, "tutorial", "declared"),
    ]
    fake["timetable"] = {
        "meetings": [
            {
                "meeting_id": f"m{i}",
                "course_id": courses[c % len(courses)]["course_id"],
                "code": courses[c % len(courses)]["code"],
                "weekday": day,
                "start": start,
                "end": end,
                "kind": kind,
                "tier": tier,
            }
            for i, (day, start, end, c, kind, tier) in enumerate(spec)
        ]
    }
    return fake


def main() -> int:
    server = subprocess.Popen(
        [sys.executable, "-c", f"from studysystem.dashboard import main; main({PORT})"]
    )
    try:
        wait_for_health()
        with urllib.request.urlopen(f"{BASE}/state") as response:
            state = json.load(response)

        problems: list[str] = []
        seen: list[str] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome")
            for width in WIDTHS:
                page = new_page(browser, width, seen)
                for route in PAGES:
                    problems += visit(page, route, f"{width}px #/{route}", seen)
                page.close()

            page = new_page(browser, WIDTHS[0], seen)
            open_page(page, "courses")
            if (cards := len(page.query_selector_all(".course"))) != len(state["courses"]):
                problems.append(f"courses: {cards} cards for {len(state['courses'])} courses")
            problems += check_today(page, state["plan"]["current"])
            problems += check_theme(page)
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
                label = f"{width}px #/plan with a review"
                problems += visit(page, "plan", label, seen)
                if not drawn_codes(page).get("Review"):
                    problems.append(f"{label}: the review item is not drawn")
                label = f"{width}px #/today with a review"
                problems += visit(page, "today", label, seen)
                lanes = page.eval_on_selector_all(
                    ".timeline .entry .eyebrow", "es => es.map(e => e.innerText.split(' · ')[0])"
                )
                if "Review" not in lanes:
                    problems.append(f"{label}: the review item is not listed")
                page.close()

            timetable = with_a_timetable(state)
            if timetable is None:
                print("no course to place a class on: the timetable blocks are not checked")
            else:
                classes = len(timetable["timetable"]["meetings"])
                for width in WIDTHS:
                    page = new_page(browser, width, seen)
                    page.route(f"{BASE}/state", lambda request: request.fulfill(json=timetable))
                    label = f"{width}px #/schedule with a timetable"
                    problems += visit(page, "schedule", label, seen)
                    drawn = len(page.query_selector_all(".week-grid .class-block"))
                    if drawn != classes:
                        problems.append(f"{label}: {drawn} blocks drawn for {classes} classes")
                    page.close()
            browser.close()

        print(f"{len(WIDTHS) * len(PAGES)} visits ({len(PAGES)} pages x {len(WIDTHS)} widths)")
        if fake:
            print(f"{len(WIDTHS) * 2} visits with a review item (plan, today)")
        for problem in problems:
            print(problem)
        print("clean" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    finally:
        server.terminate()


if __name__ == "__main__":
    sys.exit(main())
