"""Load snapshots / increments into Azure SQL with pyodbc."""

from __future__ import annotations

import os
import time
from datetime import date, datetime

import numpy as np
import pandas as pd

from .snapshot import PRIMARY_KEYS, TABLE_ORDER

BATCH = 5000


def connect(conn_str: str | None = None):
    import pyodbc  # imported lazily so CSV mode works without an ODBC driver

    conn_str = conn_str or os.environ.get("AZURE_SQL_CONN")
    if not conn_str:
        raise SystemExit("Set AZURE_SQL_CONN (see README) or pass --conn.")
    # a paused serverless database can take ~1 min to wake up: retry
    for attempt in range(1, 5):
        try:
            return pyodbc.connect(conn_str, autocommit=False)
        except pyodbc.Error as e:
            if attempt == 4:
                raise
            print(f"  connection failed (attempt {attempt}/4), retrying in 30s: {e.args[0] if e.args else e}")
            time.sleep(30)


def _py(v):
    if v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NaT:
        return None
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime().replace(microsecond=0)
    if isinstance(v, datetime):
        return v.replace(microsecond=0)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    if isinstance(v, date):
        return v
    return v


def _rows(df: pd.DataFrame):
    obj = df.astype(object).where(df.notna(), None)
    return [tuple(_py(v) for v in r) for r in obj.itertuples(index=False, name=None)]


def _insert(cur, table: str, df: pd.DataFrame):
    if df.empty:
        return
    cols = ", ".join(df.columns)
    marks = ", ".join("?" for _ in df.columns)
    sql = f"INSERT INTO {table} ({cols}) VALUES ({marks})"
    cur.fast_executemany = True
    rows = _rows(df)
    for i in range(0, len(rows), BATCH):
        cur.executemany(sql, rows[i : i + BATCH])


def load_full(cn, snap: dict[str, pd.DataFrame], seed: int, as_of: datetime):
    cur = cn.cursor()
    for t in reversed(TABLE_ORDER):
        cur.execute(f"DELETE FROM crm.{t}")
    for t in TABLE_ORDER:
        print(f"  crm.{t}: {len(snap[t]):>8,} rows")
        _insert(cur, f"crm.{t}", snap[t])
    _set_state(cur, seed, as_of)
    cn.commit()


def merge_changes(cn, delta: dict[str, pd.DataFrame], seed: int, as_of: datetime):
    cur = cn.cursor()
    for t in TABLE_ORDER:
        df = delta[t]
        print(f"  crm.{t}: {len(df):>6,} upserted")
        if df.empty:
            continue
        stg = f"staging.{t}"
        cur.execute(f"DROP TABLE IF EXISTS {stg}; SELECT TOP 0 * INTO {stg} FROM crm.{t};")
        _insert(cur, stg, df)
        pk = PRIMARY_KEYS[t]
        cols = list(df.columns)
        set_clause = ", ".join(f"tgt.{c} = src.{c}" for c in cols if c != pk)
        ins_cols = ", ".join(cols)
        ins_vals = ", ".join(f"src.{c}" for c in cols)
        cur.execute(f"""
            MERGE crm.{t} AS tgt
            USING {stg} AS src ON tgt.{pk} = src.{pk}
            WHEN MATCHED THEN UPDATE SET {set_clause}
            WHEN NOT MATCHED BY TARGET THEN INSERT ({ins_cols}) VALUES ({ins_vals});
            DROP TABLE {stg};""")
    _set_state(cur, seed, as_of)
    cn.commit()


def get_state(cn):
    cur = cn.cursor()
    row = cur.execute("SELECT seed, last_as_of FROM sim.generator_state WHERE id = 1").fetchone()
    return (row[0], row[1]) if row else (None, None)


def _set_state(cur, seed: int, as_of: datetime):
    now = datetime.utcnow().replace(microsecond=0)
    cur.execute("DELETE FROM sim.generator_state WHERE id = 1")
    cur.execute(
        "INSERT INTO sim.generator_state (id, seed, last_as_of, updated_at) VALUES (1, ?, ?, ?)",
        seed,
        as_of.replace(microsecond=0),
        now,
    )
