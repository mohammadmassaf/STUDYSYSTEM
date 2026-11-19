"""The seeded demo (4.11; D-305 - D-308): a fresh database of invented data, written through the
services, that a dashboard reads. SQLite only - `demo.build` owns a SQLite file in its own folder.
The seed runs once per module (about six seconds); each case reads what it left."""

import datetime
import os
import re

import pytest
from sqlalchemy import func, select

from studysystem import demo
from studysystem.db.tables import assessment, course, hours_entry
from studysystem.errors import StudyError
from studysystem.services import state
from studysystem.services.users import current_user

TODAY = datetime.datetime.now().astimezone().date()

# (code, partial papers, final papers): the shape the demo copies, with invented codes
PAPERS = {
    "HUM300": (0, 0),
    "ENG300": (0, 0),
    "CS301": (0, 6),
    "CS302": (2, 8),
    "CS303": (2, 7),
    "CS304": (3, 10),
    "CS305": (1, 7),
    "CS306": (1, 8),
    "CS351": (0, 0),
}
# Only the demo's own invented codes - an allowlist, so this public file names no real course.
DEMO_CODE = re.compile(r"CS3[0-9]{2}|HUM300|ENG300")
# Monday's first minutes hold no hours yet: the week is too young for its hour checks (D-305).
NEW_WEEK = TODAY.weekday() == 0 and datetime.datetime.now().astimezone().time() < datetime.time(
    0, 5
)


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """One seeded demo in a folder of its own, and the live folder it must never touch."""
    root = tmp_path_factory.mktemp("demo")
    keep = {k: os.environ.get(k) for k in (demo.DATA_ENV, demo.VAULT_ENV)}
    live = root / "live"
    live.mkdir()
    os.environ[demo.DATA_ENV] = str(live)  # what a real run's environment could name
    try:
        folder = root / "studysystem-demo"
        engine, summary, path = demo.build(folder, live=[])
        blob = state.get_state(engine, current_user(engine), TODAY, "test")
        yield {
            "engine": engine,
            "summary": summary,
            "path": path,
            "folder": folder,
            "live": live,
            "blob": blob,
        }
        engine.dispose()
    finally:
        for key, value in keep.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_the_dashboard_reads_the_demo(seeded):
    blob = seeded["blob"]
    assert len(blob["courses"]) == 9
    by_code = {c["code"]: c for c in blob["courses"]}
    assert set(by_code) == set(PAPERS)

    slots = [s for s in blob["assessment_slots"] if s["code"] != "ENG300"]
    today = TODAY.isoformat()
    for code, (_, _) in PAPERS.items():
        if code == "ENG300":
            continue
        mine = {s["name"]: s for s in slots if s["code"] == code}
        assert set(mine) == {"Partial exam", "Final exam"}, code
        assert all(s["date"] > today for s in mine.values()), code
        assert mine["Partial exam"]["weight"] == 30 and mine["Final exam"]["weight"] == 70
        assert mine["Final exam"]["date_approx"] is True
    assert [s["name"] for s in blob["assessment_slots"] if s["code"] == "ENG300"] == ["Final exam"]

    for code, (partial, final) in PAPERS.items():
        papers = by_code[code]["past_papers"]
        assert len([p for p in papers if p["assessment_name"] == "Partial exam"]) == partial, code
        assert len([p for p in papers if p["assessment_name"] == "Final exam"]) == final, code
        if final >= 3:  # enough papers for a profile: the course has a coverage reading
            assert by_code[code]["coverage"] is not None, code

    assert sum(1 for c in blob["courses"] if c["coverage"] is not None) >= 6
    assert blob["capacity"]["declared_hours_per_week"] == 10


def test_the_week_has_hours_a_streak_and_a_deviation(seeded):
    blob = seeded["blob"]
    assert blob["metrics"]["guess_sensitivity"]["shown"] > 0
    assert blob["shortfall"]["need"] is not None
    assert blob["study_time"]["streak_days"] >= 1
    if NEW_WEEK:
        pytest.skip("Monday 00:00-00:05: this week holds no hours yet")
    week = blob["metrics"]["adherence"]
    assert week["minutes"] > 0 and week["in_plan"] > 0
    assert sum(week["deviations"].values()) > 0
    assert week["missing_input"] > 0
    assert sum(blob["study_time"]["week_days"]) > 0


def test_the_demo_has_room_so_it_shows_system_work(seeded):
    """4.12's Done when, the "appears" half: the demo reads a surplus (D-310), so its Plan page
    lists CS304's zero-estimate Partial and the proposed directive (D-311)."""
    blob = seeded["blob"]
    assert blob["shortfall"]["short"] is False
    work = blob["system_work"]
    assert [(p["code"], p["reason"].startswith("CS304 Partial exam")) for p in work["papers"]] == [
        ("CS304", True)
    ]
    assert [(d["text"], d["applies_to"]) for d in work["directives"]] == [
        (demo.DIRECTIVE, demo.DIRECTIVE_COURSE)
    ]


def test_only_demo_codes_and_no_marks(seeded):
    blob = seeded["blob"]
    assert all(DEMO_CODE.fullmatch(c["code"]) for c in blob["courses"])
    assert all(s["mark"] is None for s in blob["assessment_slots"])
    with seeded["engine"].connect() as conn:
        marked = conn.execute(
            select(func.count()).select_from(assessment).where(assessment.c.mark.is_not(None))
        ).scalar_one()
    assert marked == 0


def test_seeding_twice_gives_the_same_demo(seeded, tmp_path_factory):
    def counts(summary, engine):
        with engine.connect() as conn:
            minutes = conn.execute(select(func.sum(hours_entry.c.minutes))).scalar_one()
            hours = conn.execute(select(func.count()).select_from(hours_entry)).scalar_one()
            courses = conn.execute(select(func.count()).select_from(course)).scalar_one()
        return courses, summary["papers"], summary["topics"], hours, minutes

    first = counts(seeded["summary"], seeded["engine"])
    engine, summary, _ = demo.build(tmp_path_factory.mktemp("again") / "demo", live=[])
    try:
        assert counts(summary, engine) == first
    finally:
        engine.dispose()
        os.environ[demo.DATA_ENV] = str(seeded["folder"])  # back to the shared demo's folder


def test_it_refuses_the_live_folder(tmp_path, monkeypatch):
    with pytest.raises(StudyError) as refused:
        demo.prepare(tmp_path / "data", live=[tmp_path / "data"])
    assert refused.value.code == "demo_refused"
    assert not (tmp_path / "data" / "studysystem.db").exists()

    # the folder the environment named before the call is live too
    monkeypatch.setenv(demo.DATA_ENV, str(tmp_path / "named"))
    with pytest.raises(StudyError):
        demo.prepare(tmp_path / "named")


def test_it_writes_only_inside_its_own_folder(seeded):
    folder, live = seeded["folder"], seeded["live"]
    assert list(live.iterdir()) == []  # the folder a real run would name stays empty
    assert seeded["path"] == folder / "studysystem.db"
    assert (folder / "papers").is_dir()
    assert not (folder / "vault").exists()  # no note was exported
    for path in folder.rglob("*"):  # everything the seed made is under its folder
        assert folder in path.parents


def test_old_demo_files_are_replaced(tmp_path, monkeypatch):
    monkeypatch.setenv(demo.VAULT_ENV, "")
    folder = tmp_path / "demo"
    (folder / "papers").mkdir(parents=True)
    (folder / "papers" / "old.pdf").write_bytes(b"old")
    (folder / "studysystem.db").write_bytes(b"old")
    (folder / "keep.txt").write_text("not the demo's to delete")
    demo.prepare(folder)
    assert not (folder / "papers").exists() and not (folder / "studysystem.db").exists()
    assert (folder / "keep.txt").exists()
