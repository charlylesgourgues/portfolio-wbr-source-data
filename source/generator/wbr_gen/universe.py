"""Deterministic simulation of the whole lead funnel, past AND future.

The universe holds every lead from UNIVERSE_START to UNIVERSE_END with its
full lifecycle already decided (including events after "today"). The CRM
tables are then cut "as of" a timestamp by snapshot.py. Because the universe
depends only on the seed, a daily increment is simply the difference between
two snapshots, and re-running with the same seed always gives the same data.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from . import config as C

INF = datetime(2100, 1, 1)


@dataclass
class Universe:
    teams: pd.DataFrame
    los: pd.DataFrame
    lo_segments: pd.DataFrame
    leads: pd.DataFrame
    events: pd.DataFrame
    assignments: pd.DataFrame
    locks: pd.DataFrame
    fundings: pd.DataFrame


# --------------------------------------------------------------------------- helpers
def _days(x: float) -> timedelta:
    return timedelta(seconds=float(x) * 86400)


def _lognormal(rng, median: float, sigma: float) -> float:
    return median * math.exp(sigma * rng.standard_normal())


def _rate_on(t: datetime) -> float:
    pts = C.RATE_CURVE
    if t <= pts[0][0]:
        return pts[0][1]
    for (t0, r0), (t1, r1) in zip(pts, pts[1:]):
        if t0 <= t <= t1:
            w = (t - t0).total_seconds() / (t1 - t0).total_seconds()
            return r0 + w * (r1 - r0)
    return pts[-1][1]


def _in(t: datetime, window) -> bool:
    return window[0] <= t < window[1]


# --------------------------------------------------------------------------- loan officers
class Roster:
    def __init__(self, rng: np.random.Generator):
        self.rng = rng
        self.los: list[dict] = []
        self.segments: list[tuple[int, int, datetime, datetime]] = []  # lo, team, start, end
        self._build()
        seg = pd.DataFrame(self.segments, columns=["lo_key", "team_id", "start", "end"])
        self._seg_lo = seg.lo_key.to_numpy()
        self._seg_team = seg.team_id.to_numpy()
        self._seg_start = seg.start.to_numpy(dtype="datetime64[s]")
        self._seg_end = seg.end.to_numpy(dtype="datetime64[s]")
        self.term = {lo["key"]: lo["term"] for lo in self.los}
        self.skill = {lo["key"]: lo["skill"] for lo in self.los}

    def _new_lo(self, team: int, hire: datetime) -> dict:
        rng = self.rng
        first = C.FIRST_NAMES[rng.integers(len(C.FIRST_NAMES))]
        last = C.LAST_NAMES[rng.integers(len(C.LAST_NAMES))]
        lo = dict(
            key=len(self.los),
            first=first,
            last=last,
            team=team,
            hire=hire,
            term=None,
            skill=float(np.clip(rng.normal(1.0, 0.15), 0.6, 1.4)),
            transfer=None,
        )
        self.los.append(lo)
        return lo

    def _random_term(self, lo: dict) -> datetime | None:
        start = max(lo["hire"], C.UNIVERSE_START)
        yrs = self.rng.exponential(1 / C.LO_ANNUAL_ATTRITION)
        t = start + _days(yrs * 365)
        return t if t < C.UNIVERSE_END else None

    def _build(self):
        rng = self.rng
        queue: list[dict] = []
        for team_id, *_ in C.TEAMS:
            for _ in range(C.TEAM_BASE_HEADCOUNT[team_id]):
                hire = datetime(2018, 1, 1) + _days(rng.uniform(0, (datetime(2024, 9, 15) - datetime(2018, 1, 1)).days))
                queue.append(self._new_lo(team_id, hire.replace(hour=15, minute=0, second=0, microsecond=0)))

        # planted story: Southeast attrition crunch
        d0, d1, n = C.SE_CRUNCH_DEPARTURES
        b0, b1 = C.SE_CRUNCH_BACKFILL
        crunch = [lo for lo in queue if lo["team"] == C.SE_CRUNCH_TEAM][:n]
        for lo in crunch:
            term = d0 + _days(rng.uniform(0, (d1 - d0).days))
            lo["term"] = term.replace(hour=22, minute=0, second=0, microsecond=0)
            lo["fixed"] = True
            hire = (b0 + _days(rng.uniform(0, (b1 - b0).days))).replace(hour=15, minute=0, second=0, microsecond=0)
            queue.append(self._new_lo(C.SE_CRUNCH_TEAM, hire))

        i = 0
        while i < len(queue):
            lo = queue[i]
            i += 1
            if not lo.get("fixed"):
                lo["term"] = self._random_term(lo)
                if lo["term"] is not None:
                    lo["term"] = lo["term"].replace(hour=22, minute=0, second=0, microsecond=0)
                    # a crunch-window departure would blur the planted story
                    if lo["team"] == C.SE_CRUNCH_TEAM and _in(lo["term"], C.SE_CRUNCH_WINDOW):
                        lo["term"] += timedelta(days=45)
                    hire = (lo["term"] + _days(rng.uniform(20, 45))).replace(hour=15, minute=0, second=0, microsecond=0)
                    if hire < C.UNIVERSE_END:
                        queue.append(self._new_lo(lo["team"], hire))

        # team transfers (SCD2 material). Crunch LOs never transfer.
        for lo in self.los:
            if lo.get("fixed") or rng.random() >= C.LO_TRANSFER_SHARE:
                continue
            a = max(lo["hire"], C.UNIVERSE_START) + timedelta(days=60)
            b = (lo["term"] or C.UNIVERSE_END) - timedelta(days=30)
            if b <= a:
                continue
            t = (a + _days(rng.uniform(0, (b - a).days))).replace(hour=14, minute=0, second=0, microsecond=0)
            if lo["team"] == C.SE_CRUNCH_TEAM or _in(t, C.SE_CRUNCH_WINDOW):
                continue
            other = [tid for tid, *_ in C.TEAMS if tid != lo["team"] and tid != C.SE_CRUNCH_TEAM]
            lo["transfer"] = (t, int(rng.choice(other)))

        for lo in self.los:
            end = lo["term"] or INF
            if lo["transfer"]:
                t, new_team = lo["transfer"]
                self.segments.append((lo["key"], lo["team"], lo["hire"], t))
                self.segments.append((lo["key"], new_team, t, end))
            else:
                self.segments.append((lo["key"], lo["team"], lo["hire"], end))

    def active_in_team(self, team: int, t: datetime, exclude: int | None = None) -> np.ndarray:
        t64 = np.datetime64(t, "s")
        m = (self._seg_team == team) & (self._seg_start <= t64) & (self._seg_end > t64)
        cands = self._seg_lo[m]
        if exclude is not None:
            cands = cands[cands != exclude]
        if len(cands) == 0:  # fallback: anyone active
            m = (self._seg_start <= t64) & (self._seg_end > t64)
            cands = self._seg_lo[m]
            if exclude is not None:
                cands = cands[cands != exclude]
        return cands


# --------------------------------------------------------------------------- leads
STATE_LIST = list(C.STATE_WEIGHTS)
STATE_P = np.array([C.STATE_WEIGHTS[s] for s in STATE_LIST]) / sum(C.STATE_WEIGHTS.values())
STATE_TEAM = {s: tid for tid, _, _, states in C.TEAMS for s in states}
CH_NAMES = list(C.CHANNELS)
CH_P = np.array([v[0] for v in C.CHANNELS.values()])
CB_NAMES = list(C.CREDIT_BANDS)
CB_P = np.array([v[0] for v in C.CREDIT_BANDS.values()])
MONTH_SEASON = [0.85, 0.92, 1.08, 1.15, 1.18, 1.12, 1.05, 1.00, 0.95, 0.93, 0.85, 0.75]
WEEKDAY = [1.10, 1.10, 1.08, 1.05, 1.00, 0.75, 0.60]
HOUR_W = np.array([6, 4, 3, 2, 1, 1, 1, 1, 1, 1, 1, 2, 3, 5, 7, 8, 9, 9, 9, 9, 9, 8, 8, 7], float)
HOUR_P = HOUR_W / HOUR_W.sum()


def _contact(rng, first, last):
    n = rng.integers(1, 999)
    email = f"{first.lower()}.{last.lower()}{n}@{C.EMAIL_DOMAINS[rng.integers(len(C.EMAIL_DOMAINS))]}"
    digits = (int(rng.integers(201, 990)), int(rng.integers(0, 10000)))
    return email, digits


def _fmt_email(rng, email):
    if email is None:
        return None
    r = rng.random()
    if r < 0.05:
        return email.upper()
    if r < 0.08:
        return email + " "
    if r < 0.10:
        return email.capitalize()
    return email


def _fmt_phone(rng, digits):
    if digits is None:
        return None
    area, line = digits
    f = rng.integers(3)
    if f == 0:
        return f"({area}) 555-{line:04d}"
    if f == 1:
        return f"{area}-555-{line:04d}"
    return f"+1{area}555{line:04d}"


def _new_person(rng, state, purpose):
    first = C.FIRST_NAMES[rng.integers(len(C.FIRST_NAMES))]
    last = C.LAST_NAMES[rng.integers(len(C.LAST_NAMES))]
    email, digits = _contact(rng, first, last)
    if rng.random() < 0.01:
        email = None
    if rng.random() < 0.02:
        digits = None
    mult = 1.7 if state in C.HIGH_COST_STATES else 0.75 if state in C.LOW_COST_STATES else 1.0
    amount = round(_lognormal(rng, 340_000 * mult * (0.9 if purpose == "refinance" else 1.0), 0.35), -3)
    amount = float(np.clip(amount, 60_000, 3_000_000))
    return dict(
        first=first,
        last=last,
        email=email,
        digits=digits,
        amount=amount,
        zip=C.ZIP_FIRST_DIGIT[state] + f"{rng.integers(0, 10000):04d}",
    )


def _daily_counts(rng):
    days = pd.date_range(C.UNIVERSE_START, C.UNIVERSE_END, freq="D")
    out = []
    for d in days:
        yrs = (d - pd.Timestamp(C.UNIVERSE_START)).days / 365.25
        lam = C.BASE_DAILY_LEADS * (1 + C.ANNUAL_TREND) ** yrs * MONTH_SEASON[d.month - 1] * WEEKDAY[d.weekday()]
        extra = 0
        c0, c1, vol, _ = C.PAID_SEARCH_CAMPAIGN
        if _in(d.to_pydatetime(), (c0, c1)):
            extra = rng.poisson(lam * CH_P[CH_NAMES.index("paid_search")] * vol)
        out.append((d.to_pydatetime(), int(rng.poisson(lam)), int(extra)))
    return out


def build(seed: int = 42) -> Universe:
    rng = np.random.default_rng(seed)
    roster = Roster(rng)

    leads, events, assigns, locks, fundings = [], [], [], [], []

    def ev(lead, stage, ts, source, reason=None):
        if source == "crm":
            rec = ts + timedelta(seconds=float(rng.uniform(0, 60)))
        else:
            r = rng.random()
            if r < 0.92:
                rec = ts + timedelta(minutes=float(rng.uniform(0, 10)))
            elif r < 0.985:
                rec = ts + timedelta(hours=float(rng.uniform(1, 48)))
            else:
                rec = ts + timedelta(days=float(rng.uniform(2, 10)))
        events.append(
            dict(lead=lead, stage=stage, event_ts=ts, source_system=source, recorded_at=rec, lost_reason=reason)
        )
        if source == "los" and rng.random() < 0.004:  # LOS double-post
            events.append(
                dict(
                    lead=lead,
                    stage=stage,
                    event_ts=ts,
                    source_system=source,
                    recorded_at=rec + timedelta(seconds=float(rng.uniform(1, 300))),
                    lost_reason=reason,
                )
            )

    def close_lost(lead, stage, last_ts, reason=None, explicit=None):
        explicit = (rng.random() < C.P_EXPLICIT_CLOSE) if explicit is None else explicit
        if explicit:
            ts = last_ts + _days(max(0.1, _lognormal(rng, 14, 0.8)))
            if reason is None:
                names, p = C.LOST_REASONS[stage]
                reason = str(rng.choice(names, p=p))
        else:
            ts = (last_ts + timedelta(days=C.AUTO_CLOSE_DAYS)).replace(hour=6, minute=0, second=0, microsecond=0)
            reason = "stale_auto_closed"
        ev(lead, "closed_lost", ts, "crm", reason)
        return ts

    def assign_chain(lead, team, t_start, end_ts, first_lo, rebalance_ts=None):
        cur, start, reason = first_lo, t_start, "initial"
        chain = []
        while True:
            term = roster.term[cur] or INF
            brk, nxt_reason = None, None
            if rebalance_ts and rebalance_ts > start and rebalance_ts < end_ts:
                brk, nxt_reason = rebalance_ts, "rebalance"
            if term < end_ts and term > start and (brk is None or term < brk):
                brk, nxt_reason = term, "lo_departure"
            if brk is None:
                chain.append((cur, start, None, reason))
                break
            chain.append((cur, start, brk, reason))
            if nxt_reason == "rebalance":
                rebalance_ts = None
            cands = roster.active_in_team(team, brk, exclude=cur)
            cur, start, reason = int(rng.choice(cands)), brk, nxt_reason
        for lo, a, u, r in chain:
            assigns.append(dict(lead=lead, lo_key=lo, assigned_at=a, unassigned_at=u, reason=r))
        return chain

    for day, n, extra in _daily_counts(rng):
        for k in range(n + extra):
            idx = len(leads)
            created = day + timedelta(hours=int(rng.choice(24, p=HOUR_P)), seconds=float(rng.uniform(0, 3600)))
            channel = "paid_search" if k >= n else str(rng.choice(CH_NAMES, p=CH_P))
            state = str(rng.choice(STATE_LIST, p=STATE_P))
            rate = _rate_on(created)
            refi_share = float(np.clip(0.15 + (6.8 - rate) * 0.25, 0.08, 0.45))
            purpose = "refinance" if rng.random() < refi_share else "purchase"
            band = str(rng.choice(CB_NAMES, p=CB_P))
            p = _new_person(rng, state, purpose)
            leads.append(
                dict(
                    created_at=created, person=p, channel=channel, purpose=purpose, state=state, band=band, dup_of=None
                )
            )
            # duplicate submissions (same person, messy formatting)
            if rng.random() < C.P_DUPLICATE:
                dup_created = created + timedelta(minutes=float(rng.uniform(10, 72 * 60)))
                if dup_created < C.UNIVERSE_END:
                    leads.append(
                        dict(
                            created_at=dup_created,
                            person=p,
                            channel=channel if rng.random() < 0.5 else str(rng.choice(CH_NAMES, p=CH_P)),
                            purpose=purpose,
                            state=state,
                            band=band,
                            dup_of=idx,
                        )
                    )

    # simulate lifecycles
    for i, L in enumerate(leads):
        t0 = L["created_at"]
        team = STATE_TEAM[L["state"]]
        ev(i, "lead_created", t0, "crm")
        crunch = team == C.SE_CRUNCH_TEAM and _in(t0, C.SE_CRUNCH_WINDOW)

        if L["dup_of"] is not None:
            if rng.random() < 0.25:
                t1 = t0 + timedelta(minutes=_lognormal(rng, 10, 1.0))
                lo = int(rng.choice(roster.active_in_team(team, t1)))
                end = t1 + _days(rng.uniform(1, 7))
                assign_chain(i, team, t1, end, lo)
                ev(i, "assigned", t1, "crm")
                close_lost(i, "assigned", t1, reason="duplicate", explicit=True)
            else:
                ev(i, "closed_lost", t0 + _days(rng.uniform(1, 5)), "crm", "duplicate")
            continue

        ch_mult = C.CHANNELS[L["channel"]][1]
        cb_mult = C.CREDIT_BANDS[L["band"]][1]
        if not (rng.random() < (0.85 if crunch else C.P_ASSIGN)):
            names, pr = C.LOST_REASONS["lead_created"]
            ev(i, "closed_lost", t0 + _days(rng.uniform(1, 10)), "crm", str(rng.choice(names, p=pr)))
            continue

        t1 = t0 + timedelta(minutes=_lognormal(rng, 240 if crunch else 10, 1.0))
        first_lo = int(rng.choice(roster.active_in_team(team, t1)))
        skill = roster.skill[first_lo]
        ev(i, "assigned", t1, "crm")
        stage_ts = {"assigned": t1}
        end_ts = None
        lost_stage = None
        lock_rows, fund_row = [], None

        c0, c1, _, q = C.PAID_SEARCH_CAMPAIGN
        camp = q if (L["channel"] == "paid_search" and _in(t0, (c0, c1))) else 1.0
        if rng.random() < min(0.95, C.P_PREAPPROVE * ch_mult * cb_mult * skill * camp):
            t2 = t1 + _days(max(0.08, _lognormal(rng, 3, 0.9)))
            stage_ts["pre_approved"] = t2
            p_lock = min(0.95, C.P_LOCK[L["purpose"]] * math.sqrt(ch_mult) * math.sqrt(cb_mult) * skill)
            if rng.random() < p_lock:
                med = 25 if L["purpose"] == "purchase" else 7
                t3 = t2 + _days(max(0.5, _lognormal(rng, med, 0.7 if med == 25 else 0.6)))
                stage_ts["rate_locked"] = t3
                amount = float(round(L["person"]["amount"] * rng.uniform(0.92, 1.02), -2))

                def new_lock(t, period):
                    r = _rate_on(t) + C.CREDIT_BANDS[L["band"]][2] + rng.normal(0, 0.12)
                    return dict(
                        lead=i,
                        locked_at=t,
                        period=period,
                        rate=round(r, 3),
                        amount=amount,
                        orig_expires_at=t + timedelta(days=period),
                        ext_at=None,
                        ext_days=0,
                        end_status=None,
                        end_ts=None,
                    )

                period = int(rng.choice(C.LOCK_PERIODS[0], p=C.LOCK_PERIODS[1]))
                lk = new_lock(t3, period)
                lock_rows.append(lk)
                fund_mult = {"excellent": 1.05, "good": 1.0, "fair": 0.95, "poor": 0.85}[L["band"]]
                if rng.random() < min(0.97, C.P_FUND * fund_mult):
                    need = _lognormal(rng, 33 if L["purpose"] == "purchase" else 28, 0.22)
                    if need <= period:
                        tf = t3 + _days(need)
                    elif need <= period + 15:
                        lk["ext_at"] = lk["orig_expires_at"] - _days(rng.uniform(1, 3))
                        lk["ext_days"] = 7 if need <= period + 7 else 15
                        tf = t3 + _days(need)
                    else:
                        lk["end_status"], lk["end_ts"] = "expired", lk["orig_expires_at"]
                        if rng.random() < C.P_RELOCK_AFTER_EXPIRY:
                            t3b = lk["orig_expires_at"] + _days(rng.uniform(1, 5))
                            p2 = int(rng.choice([30, 45], p=[0.6, 0.4]))
                            lk = new_lock(t3b, p2)
                            lock_rows.append(lk)
                            tf = t3b + _days(rng.uniform(10, p2 - 2))
                        else:
                            tf = None
                            lost_stage, end_ts = (
                                "rate_locked",
                                close_lost(
                                    i, "rate_locked", lk["orig_expires_at"], reason="lock_expired", explicit=True
                                ),
                            )
                    if tf is not None:
                        lk["end_status"], lk["end_ts"] = "funded", tf
                        stage_ts["funded"] = tf
                        fund_row = dict(
                            lead=i,
                            lock_ref=len(lock_rows) - 1,
                            funded_at=tf,
                            amount=amount,
                            recorded_at=tf + _days(rng.uniform(0, 2)),
                        )
                        end_ts = tf
                else:
                    tc = t3 + _days(rng.uniform(5, period - 1))
                    lk["end_status"], lk["end_ts"] = "cancelled", tc
                    names, pr = C.LOST_REASONS["rate_locked"]
                    lost_stage = "rate_locked"
                    ev(i, "closed_lost", tc + _days(rng.uniform(0, 3)), "crm", str(rng.choice(names, p=pr)))
                    end_ts = tc + timedelta(days=3)
            else:
                lost_stage = "pre_approved"
        else:
            lost_stage = "assigned"

        # lock / funded events (LOS)
        for lk in lock_rows:
            ev(i, "rate_locked", lk["locked_at"], "los")
        if "pre_approved" in stage_ts:
            ev(i, "pre_approved", stage_ts["pre_approved"], "los")
        if fund_row:
            ev(i, "funded", fund_row["funded_at"], "los")
        if end_ts is None:
            last = max(stage_ts.values())
            end_ts = close_lost(i, lost_stage, last)

        rebalance = None
        if rng.random() < C.P_REBALANCE and (end_ts - t1).days > 2:
            rebalance = t1 + _days(rng.uniform(1, (end_ts - t1).days - 1))
        assign_chain(i, team, t1, end_ts, first_lo, rebalance)

        for lk in lock_rows:
            locks.append(lk)
        if fund_row:
            fund_row["lock_obj"] = lock_rows[fund_row["lock_ref"]]
            fundings.append(fund_row)

    return _to_frames(rng, roster, leads, events, assigns, locks, fundings)


# --------------------------------------------------------------------------- frames + ids
def _to_frames(rng, roster, leads, events, assigns, locks, fundings) -> Universe:
    teams = pd.DataFrame(
        [(tid, name, region, ",".join(states), datetime(2024, 1, 1)) for tid, name, region, states in C.TEAMS],
        columns=["team_id", "team_name", "region", "covered_states", "updated_at"],
    )

    # LO ids in hire order (like an identity column)
    lo_df = pd.DataFrame(roster.los).sort_values(["hire", "key"]).reset_index(drop=True)
    lo_df["lo_id"] = np.arange(1001, 1001 + len(lo_df))
    lo_id = dict(zip(lo_df.key, lo_df.lo_id))
    lo_df["email"] = [
        f"{fn.lower()}.{ln.lower()}.{i}@{C.LO_EMAIL_DOMAIN}"
        for fn, ln, i in zip(lo_df["first"], lo_df["last"], lo_df.lo_id)
    ]
    los = lo_df.rename(columns={"first": "first_name", "last": "last_name", "hire": "hire_ts", "term": "term_ts"})[
        ["lo_id", "first_name", "last_name", "email", "hire_ts", "term_ts"]
    ]
    seg = pd.DataFrame(roster.segments, columns=["lo_key", "team_id", "start", "end"])
    seg["lo_id"] = seg.lo_key.map(lo_id)
    lo_segments = seg[["lo_id", "team_id", "start"]]

    # lead ids in creation order
    order = sorted(range(len(leads)), key=lambda i: leads[i]["created_at"])
    lead_id = {old: 100001 + new for new, old in enumerate(order)}
    rows = []
    for i, L in enumerate(leads):
        p = L["person"]
        rows.append(
            dict(
                lead_id=lead_id[i],
                created_at=L["created_at"],
                first_name=p["first"],
                last_name=p["last"],
                email=_fmt_email(rng, p["email"]),
                phone=_fmt_phone(rng, p["digits"]),
                channel=L["channel"],
                loan_purpose=L["purpose"],
                property_state=L["state"],
                property_zip=p["zip"],
                est_loan_amount=None if rng.random() < 0.03 else p["amount"],
                credit_band=None if rng.random() < 0.08 else L["band"],
                duplicate_of=lead_id[L["dup_of"]] if L["dup_of"] is not None else None,
            )
        )
    leads_df = pd.DataFrame(rows).sort_values("lead_id").reset_index(drop=True)

    a = pd.DataFrame(assigns)
    a["lead_id"] = a.lead.map(lead_id)
    a["lo_id"] = a.lo_key.map(lo_id)
    a = a.sort_values(["assigned_at", "lead_id"]).reset_index(drop=True)
    a["assignment_id"] = np.arange(1, len(a) + 1)
    assignments = a.rename(columns={"reason": "assignment_reason"})[
        ["assignment_id", "lead_id", "lo_id", "assigned_at", "unassigned_at", "assignment_reason"]
    ]

    e = pd.DataFrame(events)
    e["lead_id"] = e.lead.map(lead_id)
    e = e.sort_values(["recorded_at", "lead_id"]).reset_index(drop=True)
    e["event_id"] = np.arange(1, len(e) + 1)
    # stamp the LO owning the lead at event time
    a_sorted = assignments.sort_values(["lead_id", "assigned_at"])
    owner: dict[int, tuple[list, list]] = {}
    for lid, grp in a_sorted.groupby("lead_id"):
        owner[lid] = (list(grp.assigned_at), list(grp.lo_id))

    def _owner(lid, ts):
        if lid not in owner:
            return None
        starts, ids = owner[lid]
        k = bisect_right(starts, ts) - 1
        return int(ids[k]) if k >= 0 else None

    e["lo_id"] = [None if s == "lead_created" else _owner(lid, t) for lid, s, t in zip(e.lead_id, e.stage, e.event_ts)]
    events_df = e[["event_id", "lead_id", "stage", "event_ts", "lo_id", "source_system", "recorded_at", "lost_reason"]]

    lk = pd.DataFrame(locks)
    lk["lead_id"] = lk.lead.map(lead_id)
    lk["_obj"] = [id(x) for x in locks]
    lk = lk.sort_values(["locked_at", "lead_id"]).reset_index(drop=True)
    lk["lock_id"] = np.arange(1, len(lk) + 1)
    lock_id = dict(zip(lk._obj, lk.lock_id))
    locks_df = lk.rename(columns={"period": "lock_period_days", "rate": "interest_rate", "amount": "loan_amount"})[
        [
            "lock_id",
            "lead_id",
            "locked_at",
            "lock_period_days",
            "interest_rate",
            "loan_amount",
            "orig_expires_at",
            "ext_at",
            "ext_days",
            "end_status",
            "end_ts",
        ]
    ]

    f = pd.DataFrame(fundings)
    f["lead_id"] = f.lead.map(lead_id)
    f["lock_id"] = [lock_id[id(o)] for o in f.lock_obj]
    f = f.sort_values(["recorded_at", "lead_id"]).reset_index(drop=True)
    f["funding_id"] = np.arange(1, len(f) + 1)
    fundings_df = f.rename(columns={"amount": "funded_amount"})[
        ["funding_id", "lead_id", "lock_id", "funded_at", "funded_amount", "recorded_at"]
    ]

    return Universe(teams, los, lo_segments, leads_df, events_df, assignments, locks_df, fundings_df)
