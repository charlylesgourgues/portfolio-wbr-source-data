"""CI integration test against a throwaway SQL Server container.

  python ci/integration_check.py create-db            # create the test database
  python ci/integration_check.py verify <as_of>       # DB content == snapshot(as_of)

Run from source/. Uses AZURE_SQL_CONN (and derives a master connection from it).
"""

from __future__ import annotations

import os
import re
import sys

import pandas as pd
import pyodbc

sys.path.insert(0, "generator")
from wbr_gen import snapshot, universe  # noqa: E402

CONN = os.environ["AZURE_SQL_CONN"]


def create_db():
    db = re.search(r"Database=([^;]+)", CONN).group(1)
    master = re.sub(r"Database=[^;]+", "Database=master", CONN)
    cn = pyodbc.connect(master, autocommit=True)
    cn.execute(f"IF DB_ID('{db}') IS NULL CREATE DATABASE [{db}]")
    print(f"database {db} ready")


def verify(as_of: str):
    cn = pyodbc.connect(CONN)
    seed = cn.execute("SELECT seed FROM sim.generator_state WHERE id = 1").fetchone()[0]
    expected = snapshot.snapshot(universe.build(seed), pd.Timestamp(as_of).to_pydatetime())
    failures = 0
    for t in snapshot.TABLE_ORDER:
        pk = snapshot.PRIMARY_KEYS[t]
        n, ids_sum = cn.execute(f"SELECT COUNT(*), COALESCE(SUM(CAST({pk} AS BIGINT)), 0) FROM crm.{t}").fetchone()
        ok = n == len(expected[t]) and ids_sum == int(expected[t][pk].sum())
        failures += not ok
        print(f"  {'ok ' if ok else 'BAD'} crm.{t:<18} db={n:>7}  expected={len(expected[t]):>7}")
    # current-state columns must match too, not just row counts
    db_status = dict(cn.execute("SELECT status, COUNT(*) FROM crm.leads GROUP BY status").fetchall())
    exp_status = expected["leads"].status.value_counts().to_dict()
    ok = db_status == exp_status
    failures += not ok
    print(f"  {'ok ' if ok else 'BAD'} lead status mix db={db_status} expected={exp_status}")
    migrations = cn.execute("SELECT COUNT(*) FROM sim.schema_migrations").fetchone()[0]
    print(f"  migrations applied: {migrations}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    {"create-db": lambda: create_db(), "verify": lambda: verify(sys.argv[2])}[sys.argv[1]]()
