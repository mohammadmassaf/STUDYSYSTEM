"""Walk every dashboard route at four widths and report console errors and horizontal overflow
(3.3, D-187), then check the plan's lanes (3.4, D-190) and that the Calendar draws every
assessment on its own card (3.7, D-219). Run by hand against a database copy, never in CI:

    STUDYSYSTEM_DATA_DIR=<folder of a database copy> uv run python scripts/walk_dashboard.py

It starts the route itself, drives the installed Chrome, and exits 1 if any visit is not clean.
"""

import collections
import copy
import datetime
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
FONT_HOSTS = ("https://fonts.googleapis.com", "https://fonts.gstatic.com")
PAGES = ("overview", "queue", "calendar", "schedule", "courses", "grades", "load", "attention")


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
    if route == "overview" and page.viewport_size["width"] >= 1024:
        # D-207: Course health is a grid under the plan, not one narrow column
        columns = page.evaluate(
            "getComputedStyle(document.querySelector('.health'))"
            ".gridTemplateColumns.split(' ').length"
        )
        if columns < 2:
            problems.append(f"{label}: Course health is a single column")
    if route == "overview" and page.viewport_size["width"] <= 760:
        # D-207: the pages that do not fit the phone bar sit behind "More"
        page.click("#mobMore")
        links = page.eval_on_selector_all(
            "#mobMoreSheet a", "a => a.map(x => x.getAttribute('href'))"
        )
        if sorted(links) != ["#/attention", "#/grades", "#/load"]:
            problems.append(f"{label}: More holds {links}, not Grades, Workload and Insights")
    if route == "courses" and not page.query_selector(".meter.striped"):
        # D-19 / 3.8: coverage the server does not send stays the striped meter, never a bar
        problems.append(f"{label}: no striped coverage meter")
    if route == "queue":
        # 3.3's carried finding: a task card's text column squeezed to a thin strip at 390px
        narrow = page.eval_on_selector_all(
            ".task .mid", "mids => Math.min(...mids.map(m => m.getBoundingClientRect().width))"
        )
        if narrow < 200:
            problems.append(f"{label}: a task card's text column is {narrow:.0f}px wide")
    overflow = page.evaluate("const e = document.documentElement; e.scrollWidth - e.clientWidth")
    if overflow > 0:
        problems.append(f"{label}: scrolls sideways by {overflow}px")
    return problems


def new_page(browser, width: int, seen: list[str]):
    page = browser.new_page(viewport={"width": width, "height": HEIGHT})
    page.on("console", lambda m: seen.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: seen.append(str(e)))
    # the web fonts come from Google (D-207); a dropped font request is the network, not the page
    page.on(
        "requestfailed",
        lambda r: None if r.url.startswith(FONT_HOSTS) else seen.append(f"request failed: {r.url}"),
    )
    return page


def lane_codes(page, lane: str) -> list[str]:
    return page.eval_on_selector_all(
        f'[data-lane="{lane}"] .plan-row', "rows => rows.map(r => r.dataset.code)"
    )


def check_lanes(page, plan: dict) -> list[str]:
    """3.4's Done-when (D-190): every shown item sits in its own lane's card, the study lane as
    its entries (D-238), and a course with a first-material item - nothing taught yet - never
    shows under Study."""
    lanes = plan["lanes"]
    page.goto(f"{BASE}/#/overview")
    page.wait_for_selector("#view > *")
    problems = []
    drawn = {lane: lane_codes(page, lane) for lane in lanes}
    for lane in lanes:
        rows = lanes[lane]["entries"] if lane == "study" else lanes[lane]["items"]
        sent = [row["code"] for row in rows]
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


def with_a_timetable(state: dict) -> dict:
    """The stored state with a fixed week of classes, so the Schedule page's blocks can be
    counted whatever the copy holds. Two meetings overlap and one is inferred."""
    courses = state["courses"]
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


def drawn_sittings(page, card: str) -> collections.Counter:
    """(course code, date), counted, for every course on every row of one Calendar card;
    "" = no date. Counted, so a row drawn twice is caught."""
    pairs = page.eval_on_selector_all(
        f"#{card} tr[data-codes]",
        "rows => rows.flatMap(r => r.dataset.codes.split('|').map(c => [c, r.dataset.date]))",
    )
    return collections.Counter((code, date) for code, date in pairs)


def check_calendar(page, label: str) -> list[str]:
    """3.7's Done-when (D-219): every assessment /state sends is drawn once, on its own card - a
    coming one under Ahead, one with no date and no mark under Date not set, the rest under
    Behind you. Compared as (course, date) pairs, so a lost January row cannot hide behind the
    same course's November one."""
    page.goto(f"{BASE}/#/calendar")
    page.wait_for_selector("#view > *")
    today = page.evaluate("localISO()")
    sent = {
        "ahead": collections.Counter(),
        "undated": collections.Counter(),
        "behind": collections.Counter(),
    }
    # fetched by the page, so a faked /state is the one compared
    for a in page.evaluate("fetch('/state').then(r => r.json()).then(s => s.assessment_slots)"):
        if a["date"] is None:
            card = "undated" if a["mark"] is None else "behind"
        else:
            card = "ahead" if a["date"] >= today else "behind"
        sent[card][(a["code"], a["date"] or "")] += 1
    problems = []
    for card, pairs in sent.items():
        drawn = drawn_sittings(page, card)
        if drawn != pairs:
            missing, extra = sorted((pairs - drawn).elements()), sorted((drawn - pairs).elements())
            problems.append(f"{label} {card}: not drawn {missing}, drawn but not sent {extra}")
    january = sorted(code for code, date in drawn_sittings(page, "ahead") if date[:7] == "2027-01")
    print(f"{label}: January session drawn for {len(january)} courses {january}")
    return problems


def with_a_lab_and_a_project(state: dict) -> dict:
    """The stored state with a coming lab and a marked project with no date: the copy holds
    neither, so the "not planned" tag and a marked undated row would never be drawn."""
    fake = copy.deepcopy(state)
    first = fake["courses"][0]
    base = {**fake["assessment_slots"][0], "course_id": first["course_id"], "code": first["code"]}
    lab_day = (
        datetime.datetime.now().astimezone().date() + datetime.timedelta(days=40)
    ).isoformat()
    fake["assessment_slots"] += [
        {**base, "assessment_id": "lab", "slot_id": "lab", "name": "Lab 1", "kind": "lab"}
        | {"date": lab_day, "date_approx": False, "mark": None},
        {**base, "assessment_id": "proj", "slot_id": "proj", "name": "Project", "kind": "project"}
        | {"date": None, "date_approx": False, "mark": 15.0},
    ]
    return fake


def check_rail(page) -> list[str]:
    """D-207: the arrow collapses the rail and it stays collapsed; the mouse entering widens it."""
    problems = []
    page.goto(f"{BASE}/#/overview")
    page.wait_for_selector("#view > *")
    width = lambda: page.evaluate("document.querySelector('.rail').getBoundingClientRect().width")  # noqa: E731
    page.click("#railToggle")
    page.mouse.move(700, 400)
    page.wait_for_timeout(500)
    if width() > 80:
        problems.append(f"rail: collapsed width is {width()}px")
    page.hover(".rail")
    page.wait_for_timeout(500)
    if width() < 200:
        problems.append(f"rail: the mouse on a collapsed rail left it {width()}px wide")
    page.mouse.move(700, 400)
    page.wait_for_timeout(500)
    if width() > 80:
        problems.append(f"rail: it did not narrow again, {width()}px")
    page.click("#railToggle")
    page.mouse.move(700, 400)
    page.wait_for_timeout(500)
    if width() < 200:
        problems.append(f"rail: expanding left it {width()}px wide")
    return problems


def check_refresh(page, fake: dict) -> list[str]:
    """D-207: a plan the host makes while the tab is open shows when you return to the tab."""
    page.goto(f"{BASE}/#/overview")
    page.wait_for_selector("#view > *")
    page.route(f"{BASE}/state", lambda request: request.fulfill(json=fake))
    page.evaluate("lastLoad -= 20000; window.dispatchEvent(new Event('focus'))")
    try:
        page.wait_for_selector('[data-lane="review"] [data-uid^="review:"]', timeout=3000)
    except Exception:
        return ["refresh: a new state did not show when the tab regained focus"]
    return []


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

            if fake:
                page = new_page(browser, WIDTHS[0], seen)
                problems += check_refresh(page, fake)
                page.close()

            for width in WIDTHS if fake else ():
                page = new_page(browser, width, seen)
                page.route(f"{BASE}/state", lambda request: request.fulfill(json=fake))
                for route in ("overview", "queue"):
                    label = f"{width}px #/{route} with a review"
                    problems += visit(page, route, label, seen)
                    if not page.query_selector('[data-lane="review"] [data-uid^="review:"]'):
                        problems.append(f"{label}: the review item is not drawn")
                page.close()

            timetable = with_a_timetable(state)
            for width in WIDTHS:
                page = new_page(browser, width, seen)
                page.route(f"{BASE}/state", lambda request: request.fulfill(json=timetable))
                label = f"{width}px #/schedule with a timetable"
                problems += visit(page, "schedule", label, seen)
                drawn = (
                    page.eval_on_selector_all(".sk-week .sk-block", "blocks => blocks.length")
                    if width > 900
                    else page.eval_on_selector_all(".sk-list .sk-block", "b => b.length")
                )
                if width > 900 and drawn != len(timetable["timetable"]["meetings"]):
                    problems.append(f"{label}: {drawn} blocks drawn for 6 classes")
                page.close()

            page = new_page(browser, WIDTHS[0], seen)
            problems += check_calendar(page, "calendar")
            page.close()

            extra = with_a_lab_and_a_project(state)
            for width in WIDTHS:
                page = new_page(browser, width, seen)
                page.route(f"{BASE}/state", lambda request: request.fulfill(json=extra))
                label = f"{width}px #/calendar with a lab and a project"
                problems += visit(page, "calendar", label, seen)
                if not page.query_selector("#ahead [data-unplanned]"):
                    problems.append(f"{label}: the lab has no 'not planned' tag")
                if width == WIDTHS[0]:
                    problems += check_calendar(page, label)
                page.close()

            page = new_page(browser, WIDTHS[0], seen)
            problems += check_rail(page)
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
