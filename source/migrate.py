"""Versioned schema migrations for the Azure SQL source database.

  python migrate.py validate          # parse every migration (no database; used by CI)
  python migrate.py diagnose          # DNS + TCP reachability of the server (no login)
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


def _odbc_fields(conn: str) -> dict[str, str]:
    """Split an ODBC connection string into {key: value}, honoring {braced} values
    where ';' and '=' are literal and '}}' is an escaped '}'."""
    fields: list[str] = []
    buf: list[str] = []
    in_braces = False
    i, n = 0, len(conn)
    while i < n:
        c = conn[i]
        if c == "{" and not in_braces:
            in_braces = True
            buf.append(c)
            i += 1
        elif c == "}" and in_braces:
            if conn[i + 1 : i + 2] == "}":
                buf.append("}")
                i += 2
            else:
                in_braces = False
                buf.append(c)
                i += 1
        elif c == ";" and not in_braces:
            fields.append("".join(buf))
            buf = []
            i += 1
        else:
            buf.append(c)
            i += 1
    fields.append("".join(buf))
    out = {}
    for f in fields:
        f = f.strip()
        if "=" in f:
            k, v = f.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


def diagnose():
    """Check that the server in AZURE_SQL_CONN is reachable. Never prints credentials."""
    import socket
    import urllib.request

    conn = os.environ.get("AZURE_SQL_CONN")
    if not conn:
        sys.exit("Set AZURE_SQL_CONN.")
    if conn.strip()[:1] in "\"'":
        print("  WARNING: the connection string starts with a quote: remove the quotes from the secret")
    fields = _odbc_fields(conn)
    print(f"  keys found  : {', '.join(sorted(fields))}")
    server = next((v for k, v in fields.items() if k in ("server", "address", "addr")), None)
    if server is None:
        sys.exit("  no Server=... in the connection string")
    server = re.sub(r"(?i)^tcp:", "", server.strip("{}").replace("}}", "}"))
    host, _, port = server.partition(",")
    port = int(port or 1433)
    print(f"  server host : {host}   port: {port}")
    if not host.lower().endswith(".database.windows.net"):
        print("  WARNING: an Azure SQL host normally ends with .database.windows.net")
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as r:
            print(f"  runner IP   : {r.read().decode()}")
    except OSError:
        pass
    try:
        ips = sorted({a[4][0] for a in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)})
        print(f"  DNS         : ok -> {', '.join(ips)}")
    except socket.gaierror as e:
        sys.exit(f"  DNS         : FAILED ({e}) -> the server name in the secret is wrong")
    t = time.time()
    try:
        socket.create_connection((host, port), timeout=20).close()
        print(f"  TCP {port}    : ok in {time.time() - t:.1f}s -> the network path is open")
    except OSError as e:
        sys.exit(
            f"  TCP {port}    : FAILED after {time.time() - t:.0f}s ({e}) -> blocked before login: "
            "check 'Public network access' in the server's Networking page"
        )


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
    ap.add_argument("command", choices=["validate", "diagnose", "status", "apply"])
    {"validate": validate, "diagnose": diagnose, "status": status, "apply": apply}[ap.parse_args().command]()
