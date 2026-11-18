"""Registry access (MySQL database ingest_gw). One short connection per call."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

import pymysql
from pymysql.cursors import DictCursor

from app.config import settings

IntegrityError = pymysql.err.IntegrityError
OperationalError = pymysql.err.OperationalError
TZ = timezone(timedelta(hours=8))


def now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None, microsecond=0)


def iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=TZ).isoformat() if value else None


@contextmanager
def connect():
    m = settings()["mysql"]
    conn = pymysql.connect(host=m["host"], port=m["port"], user=m["user"], password=settings().mysql_password,
                           database=m["database"], charset="utf8mb4", cursorclass=DictCursor, autocommit=True,
                           connect_timeout=5, read_timeout=30, write_timeout=30)
    try:
        yield conn
    finally:
        conn.close()


def query(sql, args=None) -> list[dict]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return list(cur.fetchall())


def one(sql, args=None) -> dict | None:
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql, args=None) -> int:
    with connect() as conn, conn.cursor() as cur:
        return cur.execute(sql, args)


def update_record(record_id: str, **fields):
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=%s" for k in fields)
    execute(f"UPDATE ingest_record SET {cols} WHERE id=%s", [*fields.values(), record_id])
