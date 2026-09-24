"""WBR source data generator.

Examples
  python generate.py init      --target csv   --out data            # full snapshot as of now, to CSV
  python generate.py increment --target csv   --out data            # changes since last run, to CSV
  python generate.py init      --target azure                        # full load into Azure SQL (crm.*)
  python generate.py increment --target azure                        # daily upsert into Azure SQL
  python generate.py stats     --as-of 2026-09-23                    # funnel sanity check, no writes
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from wbr_gen import snapshot, universe


def _now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


def _parse(ts: str | None) -> datetime:
    return pd.Timestamp(ts).to_pydatetime() if ts else _now_utc()


def _write_csv(tables: dict[str, pd.DataFrame], folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        df.to_csv(folder / f"{name}.csv", index=False, date_format="%Y-%m-%d %H:%M:%S")
        print(f"  {name:<18} {len(df):>8,} rows")


def stats(tables: dict[str, pd.DataFrame]):
    leads, ev = tables["leads"], tables["lead_stage_events"]
    print(
        f"\nLeads: {len(leads):,}   events: {len(ev):,}   LOs: {len(tables['loan_officers'])}   "
        f"locks: {len(tables['rate_locks']):,}   fundings: {len(tables['fundings']):,}"
    )
    print("\nStatus:", leads.status.value_counts().to_dict())
    # cohort conversion on leads old enough to have matured (created >= 150 days before the cut)
    cut = leads.updated_at.max() - pd.Timedelta(days=150)
    mature = leads[leads.created_at <= cut]
    reached = ev.drop_duplicates(["lead_id", "stage"]).pivot_table(
        index="lead_id", columns="stage", values="event_id", aggfunc="count"
    )
    m = reached.reindex(mature.lead_id).notna()
    stages = ["lead_created", "assigned", "pre_approved", "rate_locked", "funded"]
    print(f"\nFunnel on {len(mature):,} mature leads (created before {cut:%Y-%m-%d}):")
    prev = None
    for s in stages:
        n = int(m[s].sum()) if s in m else 0
        step = f"  step {n / prev:6.1%}" if prev else ""
        print(f"  {s:<14} {n:>7,}  {n / len(mature):6.1%}{step}")
        prev = n or prev
    print("\nLead -> funded by channel:")
    fm = mature.assign(f=m["funded"].to_numpy())
    print(fm.groupby("channel").f.mean().map("{:.1%}".format).to_string())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["init", "increment", "stats"])
    ap.add_argument("--target", choices=["csv", "azure"], default="csv")
    ap.add_argument("--out", default="data", help="CSV output folder")
    ap.add_argument("--as-of", help="UTC timestamp to cut at (default: now)")
    ap.add_argument("--since", help="increment start (default: last run's as-of)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--conn", help="ODBC connection string (default: env AZURE_SQL_CONN)")
    args = ap.parse_args()

    as_of = _parse(args.as_of)
    out = Path(args.out)
    state_file = out / "_generator_state.json"
    seed, since = args.seed, _parse(args.since) if args.since else None

    cn = None
    if args.target == "azure" and args.command != "stats":
        from wbr_gen import azure_loader

        cn = azure_loader.connect(args.conn)
        if args.command == "increment":
            s_seed, s_last = azure_loader.get_state(cn)
            if s_seed is None:
                raise SystemExit("No generator state in Azure: run 'init' first.")
            seed, since = s_seed, since or s_last
    elif args.command == "increment":
        if not state_file.exists() and since is None:
            raise SystemExit(f"No {state_file}: run 'init' first or pass --since.")
        if state_file.exists():
            st = json.loads(state_file.read_text())
            seed, since = st["seed"], since or pd.Timestamp(st["last_as_of"]).to_pydatetime()

    t = time.time()
    print(f"Building universe (seed={seed})...")
    u = universe.build(seed)
    print(f"  done in {time.time() - t:.0f}s")

    if args.command == "stats":
        stats(snapshot.snapshot(u, as_of))
        return

    if args.command == "init":
        print(f"Full snapshot as of {as_of:%Y-%m-%d %H:%M:%S} UTC")
        snap = snapshot.snapshot(u, as_of)
        if cn:
            azure_loader.load_full(cn, snap, seed, as_of)
        else:
            _write_csv(snap, out / "full")
        stats(snap)
    else:
        if as_of <= since:
            raise SystemExit(f"Nothing to do: as-of {as_of} is not after last run {since}.")
        print(f"Changes in ({since:%Y-%m-%d %H:%M:%S}, {as_of:%Y-%m-%d %H:%M:%S}] UTC")
        delta = snapshot.changes(u, since, as_of)
        if cn:
            azure_loader.merge_changes(cn, delta, seed, as_of)
        else:
            _write_csv(delta, out / "increments" / as_of.strftime("%Y%m%dT%H%M%S"))

    if not cn:
        out.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"seed": seed, "last_as_of": as_of.isoformat()}))


if __name__ == "__main__":
    main()
