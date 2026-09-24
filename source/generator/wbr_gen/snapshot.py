"""Cut the universe "as of" a timestamp into the crm.* table shapes,
and compute the rows that changed between two timestamps (daily increment)."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from .config import STAGE_ORDER
from .universe import Universe

TABLE_ORDER = ["teams", "loan_officers", "leads", "lead_assignments", "lead_stage_events", "rate_locks", "fundings"]
PRIMARY_KEYS = {
    "teams": "team_id",
    "loan_officers": "lo_id",
    "leads": "lead_id",
    "lead_assignments": "assignment_id",
    "lead_stage_events": "event_id",
    "rate_locks": "lock_id",
    "fundings": "funding_id",
}
CHANGE_COLUMN = {  # column that tells when a row last changed
    "teams": "updated_at",
    "loan_officers": "updated_at",
    "leads": "updated_at",
    "lead_assignments": "updated_at",
    "lead_stage_events": "recorded_at",
    "rate_locks": "updated_at",
    "fundings": "recorded_at",
}


def _rowmax(*cols: pd.Series) -> pd.Series:
    return pd.concat(cols, axis=1).max(axis=1)


def snapshot(u: Universe, as_of: datetime) -> dict[str, pd.DataFrame]:
    T = pd.Timestamp(as_of)
    out: dict[str, pd.DataFrame] = {}

    out["teams"] = u.teams[u.teams.updated_at <= T].copy()

    # ---- loan officers
    lo = u.los[u.los.hire_ts <= T].copy()
    seg = u.lo_segments[u.lo_segments.start <= T].sort_values("start").groupby("lo_id").last()
    lo["team_id"] = lo.lo_id.map(seg.team_id)
    lo["seg_start"] = lo.lo_id.map(seg.start)
    termed = lo.term_ts.notna() & (lo.term_ts <= T)
    lo["hire_date"] = lo.hire_ts.dt.date
    lo["termination_date"] = lo.term_ts.where(termed).dt.date
    lo["is_active"] = (~termed).astype(int)
    lo["updated_at"] = _rowmax(lo.hire_ts, lo.seg_start, lo.term_ts.where(termed))
    out["loan_officers"] = lo[
        [
            "lo_id",
            "first_name",
            "last_name",
            "email",
            "team_id",
            "hire_date",
            "termination_date",
            "is_active",
            "updated_at",
        ]
    ]

    # ---- events known at T
    ev = u.events[u.events.recorded_at <= T]
    out["lead_stage_events"] = ev.drop(columns="lost_reason").copy()

    # ---- assignments
    a = u.assignments[u.assignments.assigned_at <= T].copy()
    a["unassigned_at"] = a.unassigned_at.where(a.unassigned_at.notna() & (a.unassigned_at <= T))
    a["updated_at"] = _rowmax(a.assigned_at, a.unassigned_at)
    out["lead_assignments"] = a

    # ---- leads (current state)
    leads = u.leads[u.leads.created_at <= T].drop(columns="duplicate_of").copy()
    prog = ev[ev.stage != "closed_lost"].assign(o=lambda d: d.stage.map(STAGE_ORDER))
    max_o = prog.groupby("lead_id").o.max()
    inv = {v: k for k, v in STAGE_ORDER.items()}
    leads["current_stage"] = leads.lead_id.map(max_o).fillna(0).astype(int).map(inv)
    closed = ev[ev.stage == "closed_lost"].sort_values("recorded_at").groupby("lead_id").last()
    funded = leads.current_stage == "funded"
    is_closed = leads.lead_id.isin(closed.index) & ~funded
    leads["status"] = np.where(funded, "funded", np.where(is_closed, "closed_lost", "open"))
    leads["lost_reason"] = leads.lead_id.map(closed.lost_reason).where(is_closed)
    fund_ts = prog[prog.stage == "funded"].groupby("lead_id").event_ts.min()
    leads["closed_at"] = leads.lead_id.map(closed.event_ts).where(is_closed)
    leads["closed_at"] = leads.closed_at.fillna(leads.lead_id.map(fund_ts).where(funded))
    cur = a[a.unassigned_at.isna()].sort_values("assigned_at").groupby("lead_id").last()
    leads["assigned_lo_id"] = leads.lead_id.map(cur.lo_id)
    last_ev = ev.groupby("lead_id").recorded_at.max()
    last_as = a.groupby("lead_id").updated_at.max()
    leads["updated_at"] = _rowmax(leads.created_at, leads.lead_id.map(last_ev), leads.lead_id.map(last_as))
    out["leads"] = leads[
        [
            "lead_id",
            "created_at",
            "first_name",
            "last_name",
            "email",
            "phone",
            "channel",
            "loan_purpose",
            "property_state",
            "property_zip",
            "est_loan_amount",
            "credit_band",
            "current_stage",
            "status",
            "lost_reason",
            "assigned_lo_id",
            "closed_at",
            "updated_at",
        ]
    ]

    # ---- rate locks
    lk = u.locks[u.locks.locked_at <= T].copy()
    ext = lk.ext_at.notna() & (lk.ext_at <= T)
    ended = lk.end_ts.notna() & (lk.end_ts <= T)
    lk["extension_days"] = np.where(ext, lk.ext_days, 0).astype(int)
    lk["expires_at"] = lk.orig_expires_at + pd.to_timedelta(lk.extension_days, unit="D")
    lk["status"] = np.where(ended, lk.end_status, np.where(ext, "extended", "active"))
    lk["updated_at"] = _rowmax(lk.locked_at, lk.ext_at.where(ext), lk.end_ts.where(ended))
    out["rate_locks"] = lk[
        [
            "lock_id",
            "lead_id",
            "locked_at",
            "lock_period_days",
            "interest_rate",
            "loan_amount",
            "expires_at",
            "extension_days",
            "status",
            "updated_at",
        ]
    ]

    out["fundings"] = u.fundings[u.fundings.recorded_at <= T].copy()

    for k, df in out.items():
        out[k] = df.sort_values(PRIMARY_KEYS[k]).reset_index(drop=True)
    return out


def changes(u: Universe, since: datetime, as_of: datetime) -> dict[str, pd.DataFrame]:
    """Rows inserted or updated in (since, as_of]."""
    snap = snapshot(u, as_of)
    S = pd.Timestamp(since)
    return {k: df[df[CHANGE_COLUMN[k]] > S].reset_index(drop=True) for k, df in snap.items()}
