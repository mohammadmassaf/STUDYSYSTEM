"""The seeded demo (4.11; D-305 - D-308): `study demo` builds a fresh database of invented data in a
folder of its own, then serves the dashboard on it. Every row goes through the service layer - the
same calls the tools make - so the demo proves the real write path, and no fixture file can drift
from the schema.

The data copies the *shape* of one real semester (how many courses, papers, topics, chapters, hours)
and nothing else: the course codes, names, instructors, targets, topic names and one-line questions
are invented (D-306). No assessment carries a mark. Dates are relative to the day it runs: exams
ahead, hours in the last 40 days, plans on past days stamped with a past clock (D-308).
"""

import datetime
import importlib.metadata
import math
import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

import platformdirs
from sqlalchemy import Engine

from studysystem.db.engine import data_dir, db_path
from studysystem.errors import StudyError
from studysystem.services import (
    assessments,
    capacity,
    courses,
    materials,
    past_exams,
    plan,
    profiles,
    state,
    study_loop,
    syllabus,
    topics,
)
from studysystem.services.generation import start_generation, submit_generation
from studysystem.services.users import ensure_user

DATA_ENV = "STUDYSYSTEM_DATA_DIR"
VAULT_ENV = "STUDYSYSTEM_VAULT_DIR"
APP_NAME = "studysystem-demo"
SEMESTER = "Demo Semester"
PARTIAL = "Partial exam"
FINAL = "Final exam"

# --- the invented semester ------------------------------------------------------------------
# Each course: the shape of one real course, with an invented name. `partial`/`final` are how many
# past papers each exam has; `approx` of the final's papers print only a month; `chapters`/`slides`
# how many materials; `topics` the textbook terms (from the papers when a slot has MIN_PAPERS or
# more, else the syllabus route, D-218).
COURSES = [
    {
        "code": "HUM300",
        "name": "Ethics",
        "credits": 3,
        "target": 75,
        "elective": True,
        "hours_per_credit": 2.7,
        "instructor": "Dr. Imani Okafor",
    },
    {
        "code": "ENG300",
        "name": "Academic Writing",
        "planned": False,
    },
    {
        "code": "CS301",
        "name": "Software Design",
        "credits": 4,
        "target": 72,
        "instructor": "Dr. Lena Ortiz",
        "partial": 0,
        "final": 6,
        "chapters": 6,
        "slides": 0,
        "topics": [
            "Requirements Elicitation",
            "UML Class Diagrams",
            "Software Process Models",
            "Design Patterns",
            "Software Testing",
            "Version Control",
            "Project Estimation",
        ],
    },
    {
        "code": "CS302",
        "name": "Web Development",
        "credits": 4,
        "target": 68,
        "instructor": "Prof. Marcus Webb",
        "lab_group": "G1",
        "partial": 2,
        "final": 8,
        "chapters": 4,
        "slides": 0,
        "topics": [
            "HTML Forms",
            "CSS Layout",
            "HTTP Methods",
            "Sessions and Cookies",
            "JavaScript Events",
            "REST APIs",
            "Web Security Basics",
        ],
    },
    {
        "code": "CS303",
        "name": "Operating Systems",
        "credits": 4,
        "target": 65,
        "instructor": "Dr. Yusuf Demir",
        "partial": 2,
        "final": 7,
        "approx": 5,
        "chapters": 2,
        "slides": 1,
        "prep_from": -14,
        "topics": [
            "Process Scheduling",
            "Threads",
            "Mutual Exclusion",
            "Semaphores",
            "Deadlock",
            "Paging",
            "Segmentation",
            "Virtual Memory",
            "Page Replacement",
            "File Systems",
            "Disk Scheduling",
            "I/O Devices",
            "System Calls",
        ],
    },
    {
        "code": "CS304",
        "name": "Computer Networks",
        "credits": 4,
        "target": 70,
        "instructor": "Prof. Anika Rao",
        "partial": 3,
        "final": 10,
        "chapters": 7,
        "slides": 2,
        "topics": [
            "OSI Model",
            "TCP/IP Stack",
            "IP Addressing",
            "Subnetting",
            "Routing Algorithms",
            "Link State Routing",
            "Distance Vector Routing",
            "TCP Congestion Control",
            "TCP Handshake",
            "UDP",
            "DNS",
            "HTTP",
            "Ethernet",
            "ARP",
            "MAC Protocols",
            "Wireless LANs",
            "Network Security",
            "NAT",
            "Error Detection",
        ],
    },
    {
        "code": "CS305",
        "name": "Databases",
        "credits": 3,
        "target": 78,
        "instructor": "Dr. Tomas Varga",
        "lab_group": "G1",
        "partial": 1,
        "final": 7,
        "approx": 3,
        "chapters": 1,
        "slides": 0,
        "topics": [
            "ER Modeling",
            "Relational Algebra",
            "SQL Joins",
            "Normalization",
            "Transactions",
            "Indexing",
            "Query Optimization",
        ],
    },
    {
        "code": "CS306",
        "name": "Algorithms",
        "credits": 3,
        "target": 62,
        "instructor": "Dr. Sofia Lindqvist",
        "partial": 1,
        "final": 8,
        "approx": 1,
        "chapters": 2,
        "slides": 3,
        "topics": [
            "Big-O Notation",
            "Sorting Algorithms",
            "Divide and Conquer",
            "Dynamic Programming",
            "Greedy Algorithms",
            "Graph Traversal",
            "Shortest Paths",
            "Minimum Spanning Trees",
            "Hash Tables",
            "Binary Search Trees",
        ],
    },
    {
        "code": "CS351",
        "name": "Machine Learning",
        "credits": 5,
        "target": 80,
        "instructor": "Prof. Dario Fontaine",
        "no_past_papers": True,
        "chapters": 3,
        "slides": 1,
        "syllabus": [
            "Linear Regression",
            "Logistic Regression",
            "Decision Trees",
            "Overfitting and Regularization",
            "Cross-Validation",
            "Neural Networks",
            "Gradient Descent",
            "Clustering",
            "Dimensionality Reduction",
        ],
    },
]

# (course, weekday 1 = Monday, start, end, kind, group)
MEETINGS = [
    ("ENG300", 1, "08:00", "10:00", "lecture", None),
    ("CS301", 1, "10:00", "11:30", "lecture", None),
    ("CS301", 3, "10:00", "11:30", "lecture", None),
    ("CS301", 5, "10:00", "11:30", "lecture", None),
    ("CS302", 1, "13:00", "15:00", "lab", "G1"),
    ("CS302", 1, "15:00", "17:00", "lab", "G1"),
    ("CS302", 2, "08:00", "10:00", "lab", "G1"),
    ("CS302", 2, "10:30", "12:00", "lecture", None),
    ("CS302", 3, "13:00", "15:00", "lab", "G1"),
    ("CS303", 2, "13:00", "14:30", "lecture", None),
    ("CS303", 3, "08:00", "09:30", "lecture", None),
    ("CS303", 5, "08:00", "09:30", "lecture", None),
    ("CS304", 4, "08:00", "09:30", "lecture", None),
    ("CS304", 4, "09:30", "11:00", "lecture", None),
    ("CS304", 5, "11:00", "12:30", "lecture", None),
    ("CS304", 5, "13:00", "15:00", "lab", None),
    ("CS305", 1, "11:30", "13:00", "lab", "G1"),
    ("CS305", 2, "15:00", "17:00", "lab", "G1"),
    ("CS305", 3, "15:00", "17:00", "lab", "G1"),
    ("CS305", 5, "15:00", "16:30", "lecture", None),
    ("CS306", 2, "14:30", "16:00", "lecture", None),
    ("CS306", 2, "16:00", "17:00", "lecture", None),
    ("CS306", 4, "13:00", "14:30", "lecture", None),
    ("CS306", 4, "14:30", "16:00", "lecture", None),
    ("CS351", 4, "11:30", "13:00", "lab", None),
    ("CS351", 4, "16:00", "17:00", "lecture", None),
    ("CS351", 5, "08:00", "10:00", "lecture", None),
]

# (hours per week, days from today): the declared capacity, insert-only (D-290)
CAPACITY = [(10, -52), (14.17, -16), (10, -3), (21, 36), (10, 47)]

# Days (from today) the plan is asked for, each stamped 08:00 that morning (D-308) - before the
# hours of that day, so an hour scoped to a shown item reads in_plan.
PLAN_DAYS = (-44, -39, -34, -29, -24, -19, -15, -11, -7, -4, -2, -1)

# The hours: (days from today, minutes, kind, deviation reason). Kind P = scoped to an item the
# plan in force showed; O = a topic it did not show; C = course level (no scope).
HOURS = [
    (-40, 60, "P", None),
    (-38, 45, "P", None),
    (-35, 90, "O", None),
    (-33, 50, "P", None),
    (-30, 60, "P", None),
    (-27, 75, "O", "disagreed"),
    (-25, 40, "P", None),
    (-22, 80, "P", None),
    (-20, 55, "O", None),
    (-17, 60, "P", None),
    (-15, 45, "P", None),
    (-13, 70, "P", None),
    (-11, 50, "O", "backlog"),
    (-9, 60, "P", None),
    (-8, 90, "P", None),
    (-6, 45, "O", None),
    (-4, 60, "P", None),
    (-3, 100, "C", None),
    (-2, 50, "P", None),
    (-2, 40, "O", "too-tired"),
    (-1, 75, "P", None),
    (0, 60, "P", None),
    (0, 45, "O", None),
    (0, 40, "C", "external-deadline"),
]

QUESTIONS = (
    "Explain {t} and give one example.",
    "Compare two approaches to {t}.",
    "Describe the main steps of {t}.",
    "List the advantages and limits of {t}.",
)


# --- the folder, and the refusal to touch the live one --------------------------------------


def demo_dir() -> Path:
    """Where the demo lives: its own app folder, never the live one."""
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))


def prepare(folder: Path, live: Sequence[Path] = ()) -> Path:
    """Point this process at `folder`, empty. Sets the data and vault folders in the environment
    before anything calls `db_path()`, so no later call can reach the live data or a real vault.
    Refuses a folder that is, or holds, a live data folder (the default one, any in `live`, and
    the one the environment named before this call). Deletes only the old demo database, its
    -wal and -shm, and the papers and materials folders - all inside `folder`. Returns the
    database path."""
    folder = folder.resolve()
    lives = [Path(platformdirs.user_data_dir("studysystem", appauthor=False)), *live]
    named = os.environ.get(DATA_ENV)
    if named:
        lives.append(Path(named))
    for other in lives:
        other = other.resolve()
        if folder == other or (other / "studysystem.db") == folder / "studysystem.db":
            raise StudyError(
                code="demo_refused",
                message=f"{folder} is the live data folder",
                fix="the demo builds its own database in a separate folder; pick another one",
            )
    folder.mkdir(parents=True, exist_ok=True)
    os.environ[DATA_ENV] = str(folder)
    os.environ[VAULT_ENV] = str(folder / "vault")  # never created: no export can write here
    path = db_path()
    assert path.parent == folder
    for name in ("studysystem.db", "studysystem.db-wal", "studysystem.db-shm"):
        (folder / name).unlink(missing_ok=True)
    for name in ("papers", "materials", "snapshots"):
        shutil.rmtree(folder / name, ignore_errors=True)
    return path


# --- tiny PDFs ------------------------------------------------------------------------------


def pdf_pages(texts: Sequence[str]) -> bytes:
    """A valid PDF with one page per text, each a text layer of one line (plain words only)."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    add(b"<< /Type /Catalog /Pages 2 0 R >>")
    add(b"")  # the page tree, filled in once the pages have numbers
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pages = []
    for text in texts:
        draw = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        stream = add(b"<< /Length %d >>\nstream\n" % len(draw) + draw + b"\nendstream")
        pages.append(
            add(
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R"
                b" /Resources << /Font << /F1 %d 0 R >> >> >>" % (stream, font)
            )
        )
    kids = b" ".join(b"%d 0 R" % p for p in pages)
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(pages))

    out = b"%PDF-1.4\n"
    offsets = []
    for num, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return out


def _write(folder: Path, name: str, texts: Sequence[str]) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(pdf_pages(texts))
    return str(path)


# --- the seed -------------------------------------------------------------------------------


def _utc(local: datetime.datetime) -> str:
    """A naive local moment as the UTC timestamp string `ids.now()` returns (D-308)."""
    return local.astimezone(datetime.UTC).strftime(study_loop.TIMESTAMP)


def _paper_date(i: int, slot: str, approx: bool) -> tuple[str, str]:
    """(session type, date) of the i-th paper, newest year first: a first and a second session per
    year, 2025 downwards. Just dates - the papers say nothing."""
    year = 2025 - i // 2
    first = i % 2 == 0
    month = ("02" if first else "09") if slot == FINAL else ("03" if first else "10")
    kind = "first" if first else "second"
    return kind, f"{year}-{month}" if approx else f"{year}-{month}-{5 + 2 * i:02d}"


def _items(slot: str, p: int, names: Sequence[str]) -> list[dict]:
    """The transcribed questions of the p-th paper of a slot: six on the final, five on a
    partial, stepping through the topics so the first papers touch every one."""
    n = len(names)
    out = []
    for j in range(6 if slot == FINAL else 5):
        topic_name = names[(p * 2 + j) % n if slot == FINAL else (p + j) % min(n, 6)]
        marks = [10, 15, 20, 10, 15, 20][j]
        out.append(
            {
                "position": j + 1,
                "question": QUESTIONS[(p + j) % 4].format(t=topic_name),
                "answer_key": None,
                "marks": None if (p % 3 == 0 and j % 2 == 1) else marks,
                "topic": {"proposed_name": topic_name},
            }
        )
    return out


def _accept_proposed(engine: Engine, uid: str, code: str) -> dict[str, str]:
    """Accept every proposed topic of the course. Returns {name: topic_id} of its topics."""
    found = topics.get_topics(engine, uid, code)["topics"]
    for t in found:
        if t["status"] == "proposed":
            topics.confirm_topic_proposal(engine, uid, t["id"], "accept")
    return {t["name"]: t["id"] for t in found}


def _papers(engine: Engine, uid: str, spec: dict, work: Path) -> int:
    """Register and transcribe the course's past papers, accept the topics, derive profiles."""
    code, names = spec["code"], spec["topics"]
    count = 0
    for slot, n in ((PARTIAL, spec["partial"]), (FINAL, spec["final"])):
        approx_n = spec.get("approx", 0) if slot == FINAL else 0
        for i in range(n):
            kind, date = _paper_date(i, slot, approx=i >= n - approx_n)
            path = _write(
                work / code / slot,
                f"{code}_{date}_{i}.pdf",
                [f"Invented {slot} paper {code} {date}"],
            )
            paper = past_exams.add_past_exam(engine, uid, code, slot, kind, date, path)
            # Slots with fewer than MIN_PAPERS papers stay registered, untranscribed: the plan
            # then asks for them, as it does for a real course still being set up.
            if n >= profiles.MIN_PAPERS:
                task = start_generation(engine, uid, "transcription", paper["past_exam_id"])
                reply = submit_generation(
                    engine,
                    uid,
                    task.payload["task_id"],
                    {"items": _items(slot, i, names)},
                )
                assert reply["status"] == "closed", reply
            count += 1
    _accept_proposed(engine, uid, code)
    for slot, n in ((PARTIAL, spec["partial"]), (FINAL, spec["final"])):
        if n >= profiles.MIN_PAPERS:
            profiles.derive_profile(engine, uid, code, slot)
    return count


def _materials(engine: Engine, uid: str, spec: dict, topic_ids: dict[str, str], work: Path) -> int:
    """Add the course's chapters and slides, tag each to a slice of its topics, write a note for
    every chapter but the last of a course with three or more (so the course keeps one untaught
    chapter, as a real one does mid-term)."""
    code = spec["code"]
    files = [("chapter", n) for n in range(1, spec["chapters"] + 1)] + [
        ("slides", n) for n in range(1, spec["slides"] + 1)
    ]
    ids = list(topic_ids.values())
    per = max(1, math.ceil(len(ids) / len(files)))
    for index, (kind, n) in enumerate(files):
        label = "Chapter" if kind == "chapter" else "Slides"
        topic_name = list(topic_ids)[min(index * per, len(ids) - 1)].replace("/", "-")
        name = f"{code} {label} {n} {topic_name}.pdf"
        path = _write(
            work / code / "materials",
            name,
            [f"{label} {n} of {code} introduces {topic_name} with one worked example"],
        )
        added = materials.add_material(engine, uid, code, path, kind)
        chosen = ids[index * per : (index + 1) * per] or [ids[index % len(ids)]]
        task = start_generation(engine, uid, "tagging", material_id=added["material_id"])
        submit_generation(
            engine, uid, task.payload["task_id"], {"topics": [{"id": t} for t in chosen]}
        )
        last_chapter = kind == "chapter" and n == spec["chapters"]
        if kind == "chapter" and not (spec["chapters"] >= 3 and last_chapter):
            task = start_generation(engine, uid, "note", material_id=added["material_id"])
            body = f"# {label} {n}\n\nKey idea: {topic_name}. Invented demo note."
            submit_generation(
                engine, uid, task.payload["task_id"], {"notes": [{"section": 1, "body": body}]}
            )
    return len(files)


def _hour(engine, uid, shown, entry, k, started_at, minutes, topic_ids):
    """Log one hour of the plan's kind (see HOURS). Returns the minutes logged."""
    _, _, kind, reason = entry
    planned = [c["code"] for c in COURSES if c.get("planned", True) and c["code"] != "ENG300"]
    hint = planned[k % len(planned)]
    scope: dict = {}
    code = hint
    if kind == "P" and shown:
        mine = [i for i in shown if i.get("code") == hint] or shown
        item = mine[k % len(mine)]
        code = item["code"]
        if item.get("setup_key"):
            scope = {"setup_key": item["setup_key"]}
        elif item.get("material_id"):
            scope = {"material_id": item["material_id"]}
        else:
            scope = {"topic_id": item["topic_id"]}
    elif kind == "O" and topic_ids.get(hint):
        # a topic of the course the plan did not show
        seen = {i.get("topic_id") for i in shown}
        free = [t for t in topic_ids[hint].values() if t not in seen]
        if free:
            scope = {"topic_id": free[k % len(free)]}
    study_loop.log_hours(
        engine,
        uid,
        code,
        minutes=minutes,
        started_at=started_at,
        deviation_reason=reason,
        **scope,
    )
    return minutes


def seed(engine: Engine, today: datetime.date | None = None) -> dict:
    """Write the whole demo through the services and check it reads back (`get_state`). Returns a
    summary: counts of what was written. `today` defaults to the machine's date."""
    now_local = datetime.datetime.now().astimezone()
    today = today or now_local.date()
    uid = ensure_user(engine)

    def day(offset: int) -> datetime.date:
        return today + datetime.timedelta(days=offset)

    summary: dict = {
        "courses": 0,
        "papers": 0,
        "materials": 0,
        "hours": 0,
        "minutes": 0,
        "plans": 0,
    }
    topic_ids: dict[str, dict[str, str]] = {}
    work = Path(tempfile.mkdtemp(prefix="studysystem-demo-"))
    try:
        for spec in COURSES:
            code = spec["code"]
            courses.add_course(engine, uid, code, spec["name"], SEMESTER, spec.get("planned", True))
            summary["courses"] += 1
            for field in ("credits", "instructor", "lab_group", "hours_per_credit"):
                if field in spec:
                    courses.set_course_input(engine, uid, code, field, spec[field])
            if "target" in spec:
                courses.set_course_input(engine, uid, code, "target_grade", spec["target"])
            if spec.get("elective"):
                courses.set_course_input(engine, uid, code, "elective", True)
            if spec.get("no_past_papers"):
                courses.set_course_input(engine, uid, code, "no_past_papers", True)
            if not spec.get("planned", True):
                continue  # his timetable only: one final, weight inferred, no date
            assessments.add_assessment(engine, uid, code, PARTIAL, "exam", 30)
            assessments.set_assessment_input(
                engine, uid, code, PARTIAL, "date", day(19).isoformat()
            )
            if "prep_from" in spec:
                prep = day(spec["prep_from"]).isoformat()
                assessments.set_assessment_input(engine, uid, code, PARTIAL, "prep_from", prep)
            assessments.set_assessment_input(engine, uid, code, FINAL, "weight", 70)
            assessments.set_assessment_input(
                engine, uid, code, FINAL, "date", f"~{day(61).isoformat()}"
            )

            if "topics" in spec:
                summary["papers"] += _papers(engine, uid, spec, work)
                topic_ids[code] = _accept_proposed(engine, uid, code)
            elif "syllabus" in spec:
                syllabus.add_syllabus_topics(engine, uid, code, FINAL, spec["syllabus"])
                topic_ids[code] = _accept_proposed(engine, uid, code)
            if code in topic_ids and spec.get("chapters", 0) + spec.get("slides", 0):
                summary["materials"] += _materials(engine, uid, spec, topic_ids[code], work)

        # one timetable task for the whole week
        task = start_generation(engine, uid, "timetable")
        meetings = []
        for code, weekday, start, end, kind, group in MEETINGS:
            m = {
                "course_code": code,
                "weekday": weekday,
                "start_time": start,
                "end_time": end,
                "kind": kind,
            }
            if group:
                m["group_label"] = group
            meetings.append(m)
        reply = submit_generation(engine, uid, task.payload["task_id"], {"meetings": meetings})
        assert reply["status"] == "closed", reply
        summary["meetings"] = len(meetings)

        for hours, offset in CAPACITY:
            capacity.set_capacity(engine, uid, hours, day(offset).isoformat())

        # Past days, oldest first: the plan of the morning (stamped 08:00), then that day's hours.
        shown: list[dict] = []
        elapsed = now_local.hour * 60 + now_local.minute  # minutes of today gone
        by_day: dict[int, list] = {}
        for k, entry in enumerate(HOURS):
            by_day.setdefault(entry[0], []).append((k, entry))
        for offset in range(-44, 1):
            if offset in PLAN_DAYS:
                clock = datetime.datetime.combine(day(offset), datetime.time(8, 0))
                made = plan.get_plan(engine, uid, day(offset), now=_utc(clock))
                shown = [i for lane in made["lanes"].values() for i in lane["items"]]
                summary["plans"] += 1
            for slot_no, (k, entry) in enumerate(by_day.get(offset, [])):
                _, minutes, _, _ = entry
                if offset == 0:
                    minutes = min(minutes, elapsed - 2)  # a start still today (date alone = now)
                    if minutes < 1:
                        continue
                    started = day(0).isoformat()
                else:
                    started = f"{day(offset).isoformat()}T{17 + 2 * slot_no:02d}:00"
                summary["minutes"] += _hour(
                    engine, uid, shown, entry, k, started, minutes, topic_ids
                )
                summary["hours"] += 1
        plan.get_plan(engine, uid, today)  # today's plan, on the real clock
        summary["plans"] += 1
    finally:
        shutil.rmtree(work, ignore_errors=True)

    summary["topics"] = {code: len(found) for code, found in topic_ids.items()}
    # self-check: every section of the dashboard's blob builds and passes its schema
    blob = state.get_state(engine, uid, today, importlib.metadata.version("studysystem"))
    summary["state_sections"] = sorted(
        k for k in blob if k not in ("server_version", "generated_at")
    )
    return summary


def build(folder: Path | None = None, live: Sequence[Path] = ()) -> tuple[Engine, dict, Path]:
    """Prepare the demo folder, migrate a fresh database, seed it. Returns (engine, summary,
    database path)."""
    from studysystem.db import migrate
    from studysystem.db.engine import make_engine

    path = prepare(folder or demo_dir(), live)
    assert data_dir() == path.parent
    engine = make_engine(path)
    migrate.upgrade(engine)
    return engine, seed(engine), path
