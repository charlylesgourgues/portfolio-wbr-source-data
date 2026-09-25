"""Versioned schema migrations for the Azure SQL source database.

  python migrate.py validate          # parse every migration (no database; used by CI)
  python migrate.py status            # list applied / pending migrations
  python migrate.py apply             # apply pending migrations (used by CD)

Migrations are files sql/migrations/V<nnn>__<name>.sql, applied in order,
each in its own transaction, and recorded in sim.schema_migrations with a
checksum. Editing an already-applied file makes status/apply fail: add a new
migration instead.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
from pathlib import Path

MIGRATIONS = Path(__file__).parent / "sql" / "migrations"
NAME = re.compile(r"^V(\d{3})__([a-z0-9_]+)\.sql$")
GO = re.compile(r"^\s*GO\s*$", re.IGNORECASE | re.MULTILINE)

BOOTSTRAP = """
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'sim') EXEC('CREATE SCHEMA sim');
IF OBJECT_ID('sim.schema_migrations') IS NULL
    CREATE TABLE sim.schema_migrations (
        version     INT           NOT NULL PRIMARY KEY,
        name        NVARCHAR(200) NOT NULL,
        checksum    CHAR(64)      NOT NULL,
        applied_at  DATETIME2(0)  NOT NULL DEFAULT SYSUTCDATETIME()
    );
"""


def discover() -> list[tuple[int, str, Path, str]]:
    out = []
    for p in sorted(MIGRATIONS.glob("*.sql")):
        m = NAME.match(p.name)
        if not m:
            sys.exit(f"Bad migration file name: {p.name} (expected V001__snake_case.sql)")
        out.append((int(m.group(1)), p.name, p, hashlib.sha256(p.read_bytes()).hexdigest()))
    versions = [v for v, *_ in out]
    if versions != list(range(1, len(versions) + 1)):
        sys.exit(f"Migration versions must be contiguous from V001, got {versions}")
    return out


def batches(sql: str) -> list[str]:
    return [b.strip() for b in GO.split(sql) if b.strip()]


def validate():
    import logging

    import sqlglot

    logging.getLogger("sqlglot").setLevel(logging.ERROR)  # GRANT/IF are parsed as opaque commands
    for _v, name, path, _ in discover():
        n = 0
        for b in batches(path.read_text()):
            body = re.sub(r"/\*.*?\*/", "", b, flags=re.S)
            if "DROP TABLE" in body.upper():
                sys.exit(f"{name}: DROP TABLE is not allowed in a migration run by CD")
            n += len([s for s in sqlglot.parse(body, read="tsql") if s])
        print(f"  ok  {name} ({n} statements)")


def _connect():
    import pyodbc

    conn = os.environ.get("AZURE_SQL_CONN")
    if not conn:
        sys.exit("Set AZURE_SQL_CONN.")
    # a paused serverless database can take ~1 min to wake up: retry
    for attempt in range(1, 5):
        try:
            return pyodbc.connect(conn, autocommit=False, timeout=60)
        except pyodbc.Error as e:
            if attempt == 4:
                raise
            print(f"  connection failed (attempt {attempt}/4), retrying in 30s: {e.args[-1] if e.args else e}")
            time.sleep(30)


def _applied(cur) -> dict[int, tuple[str, str]]:
    cur.execute(BOOTSTRAP)
    cur.connection.commit()
    return {r[0]: (r[1], r[2]) for r in cur.execute("SELECT version, name, checksum FROM sim.schema_migrations")}


def _plan(cur):
    applied = _applied(cur)
    pending = []
    for v, name, path, checksum in discover():
        if v in applied:
            if applied[v][1] != checksum:
                sys.exit(f"{name} was modified after being applied. Revert it and add a new migration.")
        else:
            pending.append((v, name, path, checksum))
    return applied, pending


def status():
    cur = _connect().cursor()
    applied, pending = _plan(cur)
    for _v, (name, _) in sorted(applied.items()):
        print(f"  applied  {name}")
    for _, name, *_ in pending:
        print(f"  PENDING  {name}")


def apply():
    cn = _connect()
    cur = cn.cursor()
    _, pending = _plan(cur)
    if not pending:
        print("  schema up to date")
    for v, name, path, checksum in pending:
        try:
            for b in batches(path.read_text()):
                cur.execute(b)
            cur.execute(
                "INSERT INTO sim.schema_migrations (version, name, checksum) VALUES (?, ?, ?)", v, name, checksum
            )
            cn.commit()
            print(f"  applied  {name}")
        except Exception:
            cn.rollback()
            print(f"  FAILED   {name} (rolled back)")
            raise


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["validate", "status", "apply"])
    {"validate": validate, "status": status, "apply": apply}[ap.parse_args().command]()
