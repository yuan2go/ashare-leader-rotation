from contextlib import closing, contextmanager
import os
from pathlib import Path
import sqlite3


@contextmanager
def read_snapshot(path: Path):
    """Hold one read transaction across related queries."""
    timeout_ms = min(120_000, max(1_000, int(os.environ.get("PHASE7A_SQLITE_TIMEOUT_MS", "30000"))))
    with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True, timeout=timeout_ms/1000)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        yield db


def rows(db, sql, parameters=()):
    return [dict(row) for row in db.execute(sql, parameters)]
