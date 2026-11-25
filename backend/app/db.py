from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3


@contextmanager
def read_snapshot(path: Path):
    """Hold one read transaction across related queries."""
    with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        yield db


def rows(db, sql, parameters=()):
    return [dict(row) for row in db.execute(sql, parameters)]
