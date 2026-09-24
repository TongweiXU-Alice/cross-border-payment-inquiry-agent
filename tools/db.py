"""Read-only SQLite access for the synthetic demo."""

import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional, Sequence
from urllib.parse import quote


DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "payments.db"
# Kept for import compatibility. Runtime access resolves PAYMENT_AGENT_DB on
# every connection so tests and callers can safely inject a temporary database.
DB = DEFAULT_DB


class DatabaseError(RuntimeError):
    """Base class for controlled data-source failures."""


class DatabaseUnavailableError(DatabaseError):
    pass


class DatabaseSchemaError(DatabaseError):
    pass


def get_db_path() -> Path:
    configured = os.getenv("PAYMENT_AGENT_DB")
    return Path(configured).expanduser().resolve() if configured else DEFAULT_DB


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(db_path).resolve() if db_path else get_db_path()
    if not path.is_file():
        raise DatabaseUnavailableError("Synthetic SQLite database is unavailable.")
    uri = "file:{}?mode=ro".format(quote(str(path), safe="/"))
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        con.execute("PRAGMA foreign_keys=ON")
        return con
    except sqlite3.Error as exc:
        raise DatabaseUnavailableError("Synthetic SQLite database could not be opened.") from exc


def rows(sql: str, params: Sequence[Any] = ()) -> List[dict]:
    try:
        with connect() as con:
            return [dict(row) for row in con.execute(sql, params).fetchall()]
    except DatabaseError:
        raise
    except sqlite3.OperationalError as exc:
        raise DatabaseSchemaError("Synthetic SQLite schema is missing or incompatible.") from exc
    except sqlite3.DatabaseError as exc:
        raise DatabaseUnavailableError("Synthetic SQLite database is unreadable.") from exc


def assert_schema(required_tables: Optional[Iterable[str]] = None) -> None:
    expected = set(required_tables or {
        "transactions", "bank_status", "refunds", "fees", "documents", "sop",
    })
    found = {
        row["name"]
        for row in rows("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = expected - found
    if missing:
        raise DatabaseSchemaError(
            "Synthetic SQLite schema is missing required tables: {}".format(
                ", ".join(sorted(missing))
            )
        )
