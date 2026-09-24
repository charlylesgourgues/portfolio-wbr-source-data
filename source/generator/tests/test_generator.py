"""Data contract tests for the source simulator.

The universe takes ~25 s to build, so it is built once per session."""

from __future__ import annotations

import hashlib
from datetime import datetime

import pandas as pd
import pytest
from wbr_gen import snapshot, universe

AS_OF = datetime(2026, 9, 22, 6, 0)
SE_STATES = ["GA", "NC", "SC", "TN", "AL", "MS", "VA"]


@pytest.fixture(scope="session")
def u():
    return universe.build(42)


@pytest.fixture(scope="session")
def snap(u):
    return snapshot.snapshot(u, AS_OF)


def _fingerprint(df: pd.DataFrame) -> str:
    return hashlib.sha256(pd.util.hash_pandas_object(df.astype(str), index=False).values.tobytes()).hexdigest()


# ------------------------------------------------------------------ structure
@pytest.mark.parametrize("table", snapshot.TABLE_ORDER)
def test_primary_keys_unique(snap, table):
    pk = snapshot.PRIMARY_KEYS[table]
    assert snap[table][pk].notna().all()
    assert snap[table][pk].is_unique


def test_foreign_keys(snap):
    lo, leads = set(snap["loan_officers"].lo_id), set(snap["leads"].lead_id)
    assert set(snap["loan_officers"].team_id) <= set(snap["teams"].team_id)
    assert set(snap["leads"].assigned_lo_id.dropna()) <= lo
    for t in ["lead_assignments", "lead_stage_events", "rate_locks", "fundings"]:
        assert set(snap[t].lead_id) <= leads, t
    assert set(snap["lead_assignments"].lo_id) <= lo
    assert set(snap["lead_stage_events"].lo_id.dropna()) <= lo
    assert set(snap["fundings"].lock_id) <= set(snap["rate_locks"].lock_id)


def test_nothing_from_the_future(snap):
    T = pd.Timestamp(AS_OF)
    assert (snap["leads"].updated_at <= T).all()
    assert (snap["lead_stage_events"].recorded_at <= T).all()
    assert (snap["rate_locks"].updated_at <= T).all()
    assert (snap["fundings"].recorded_at <= T).all()


def test_domain_values(snap):
    leads = snap["leads"]
    assert set(leads.status) <= {"open", "funded", "closed_lost"}
    assert set(leads.current_stage) <= set(universe.C.STAGES)
    assert set(snap["rate_locks"].status) <= {"active", "extended", "funded", "expired", "cancelled"}
    assert (leads[leads.status == "funded"].current_stage == "funded").all()
    assert leads[leads.status == "closed_lost"].lost_reason.notna().all()


# ------------------------------------------------------------------ incremental loads
@pytest.mark.parametrize("days", [1, 3])
def test_full_plus_increment_equals_next_full(u, days):
    t1 = AS_OF
    t2 = t1 + pd.Timedelta(days=days)
    s1, s2 = snapshot.snapshot(u, t1), snapshot.snapshot(u, t2)
    delta = snapshot.changes(u, t1, t2)
    for t in snapshot.TABLE_ORDER:
        pk = snapshot.PRIMARY_KEYS[t]
        merged = pd.concat([s1[t][~s1[t][pk].isin(delta[t][pk])], delta[t]])
        merged = merged.sort_values(pk).reset_index(drop=True)
        assert merged.astype(str).equals(s2[t].astype(str)), t


def test_daily_increment_is_small_but_not_empty(u):
    delta = snapshot.changes(u, AS_OF, AS_OF + pd.Timedelta(days=1))
    assert 50 < len(delta["leads"]) < 1000
    assert len(delta["lead_stage_events"]) > 50


# ------------------------------------------------------------------ calibration
def test_funnel_rates_in_expected_bands(snap):
    leads, ev = snap["leads"], snap["lead_stage_events"]
    mature = leads[leads.created_at <= pd.Timestamp(AS_OF) - pd.Timedelta(days=150)]
    reached = ev.drop_duplicates(["lead_id", "stage"]).groupby("stage").lead_id.apply(set)
    rate = {s: mature.lead_id.isin(reached[s]).mean() for s in universe.C.STAGES}
    assert 0.88 < rate["assigned"] < 0.95
    assert 0.14 < rate["pre_approved"] < 0.22
    assert 0.05 < rate["rate_locked"] < 0.10
    assert 0.04 < rate["funded"] < 0.08


def test_data_quality_defects_present(snap):
    ev, leads = snap["lead_stage_events"], snap["leads"]
    assert ev.duplicated(["lead_id", "stage", "event_ts"]).sum() > 10  # double posts
    late = (ev.recorded_at - ev.event_ts) > pd.Timedelta(days=2)
    assert late.sum() > 100  # late arrivals
    assert (leads.lost_reason == "duplicate").sum() > 500  # duplicate leads
    assert leads.email.dropna().str.contains(r"[A-Z]|\s$").any()  # messy emails


# ------------------------------------------------------------------ planted stories
def test_story_paid_search_campaign(snap):
    leads, ev = snap["leads"], snap["lead_stage_events"]
    pre = set(ev[ev.stage == "pre_approved"].lead_id)
    ps = leads[leads.channel == "paid_search"]
    inside = ps[(ps.created_at >= "2025-05-05") & (ps.created_at < "2025-06-15")]
    before = ps[(ps.created_at >= "2025-03-20") & (ps.created_at < "2025-05-01")]
    assert len(inside) / 41 > 1.4 * len(before) / 42
    assert inside.lead_id.isin(pre).mean() < 0.6 * before.lead_id.isin(pre).mean()


def test_story_southeast_crunch(snap):
    leads, ev = snap["leads"], snap["lead_stage_events"]
    asg = ev[ev.stage == "assigned"].groupby("lead_id").event_ts.min()
    se = leads[leads.property_state.isin(SE_STATES)].assign(asg=lambda d: d.lead_id.map(asg))
    se["minutes"] = (se.asg - se.created_at).dt.total_seconds() / 60
    jan = se[(se.created_at >= "2026-01-05") & (se.created_at < "2026-02-01")].minutes.median()
    feb = se[(se.created_at >= "2026-02-02") & (se.created_at < "2026-02-28")].minutes.median()
    assert feb > 10 * jan


# ------------------------------------------------------------------ determinism
def test_same_seed_same_data(snap):
    again = snapshot.snapshot(universe.build(42), AS_OF)
    for t in snapshot.TABLE_ORDER:
        assert _fingerprint(snap[t]) == _fingerprint(again[t]), t
