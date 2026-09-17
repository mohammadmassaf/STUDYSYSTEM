"""Not a test. Child process for the two-writers test: insert one row, exit 0."""

import sys
from pathlib import Path

from studysystem.db.engine import make_engine

engine = make_engine(Path(sys.argv[1]))
with engine.begin() as conn:
    conn.exec_driver_sql("CREATE TABLE IF NOT EXISTS smoke (id INTEGER PRIMARY KEY)")
    conn.exec_driver_sql("INSERT INTO smoke DEFAULT VALUES")
