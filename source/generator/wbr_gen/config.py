"""Calibration of the synthetic US mortgage lead funnel.

Everything here is synthetic. Rates, volumes and conversion rates are
plausible orders of magnitude for a lead-marketplace mortgage lender,
not real figures from any company.
"""

from datetime import datetime

UNIVERSE_START = datetime(2024, 10, 1)
UNIVERSE_END = datetime(2027, 12, 31)  # leads are simulated up to here; snapshots cut "as of" a date

BASE_DAILY_LEADS = 68  # before seasonality / weekday / trend
ANNUAL_TREND = 0.08

STAGES = ["lead_created", "assigned", "pre_approved", "rate_locked", "funded"]
STAGE_ORDER = {s: i for i, s in enumerate(STAGES)}

# ---------------------------------------------------------------- teams
TEAMS = [
    # id, name, region, states
    (1, "Pacific", "West", ["CA", "OR", "WA", "NV", "HI", "AK"]),
    (2, "Mountain", "West", ["AZ", "CO", "UT", "ID", "MT", "WY", "NM"]),
    (3, "Texas & South Central", "Southwest", ["TX", "OK", "LA", "AR"]),
    (4, "Midwest", "Midwest", ["IL", "OH", "MI", "IN", "WI", "MN", "MO", "IA", "KS", "NE", "ND", "SD", "KY"]),
    (5, "Florida", "Southeast", ["FL"]),
    (6, "Southeast", "Southeast", ["GA", "NC", "SC", "TN", "AL", "MS", "VA"]),
    (7, "Northeast", "Northeast", ["NY", "NJ", "PA", "MA", "CT", "MD", "DC", "DE", "RI", "NH", "VT", "ME", "WV"]),
]
TEAM_BASE_HEADCOUNT = {1: 22, 2: 12, 3: 18, 4: 20, 5: 14, 6: 16, 7: 20}
LO_ANNUAL_ATTRITION = 0.12
LO_TRANSFER_SHARE = 0.08

# rough population weights (millions) used to spread leads across states
STATE_WEIGHTS = {
    "CA": 39,
    "TX": 30,
    "FL": 22,
    "NY": 19.5,
    "PA": 13,
    "IL": 12.5,
    "OH": 11.8,
    "GA": 11,
    "NC": 10.8,
    "MI": 10,
    "NJ": 9.3,
    "VA": 8.7,
    "WA": 7.8,
    "AZ": 7.4,
    "TN": 7.1,
    "MA": 7,
    "IN": 6.8,
    "MD": 6.2,
    "MO": 6.2,
    "WI": 5.9,
    "CO": 5.9,
    "MN": 5.7,
    "SC": 5.3,
    "AL": 5.1,
    "LA": 4.6,
    "KY": 4.5,
    "OR": 4.2,
    "OK": 4,
    "CT": 3.6,
    "UT": 3.4,
    "IA": 3.2,
    "NV": 3.2,
    "AR": 3,
    "MS": 2.9,
    "KS": 2.9,
    "NM": 2.1,
    "NE": 2,
    "ID": 1.9,
    "WV": 1.8,
    "HI": 1.4,
    "NH": 1.4,
    "ME": 1.4,
    "MT": 1.1,
    "RI": 1.1,
    "DE": 1,
    "SD": 0.9,
    "ND": 0.8,
    "AK": 0.7,
    "DC": 0.7,
    "VT": 0.6,
    "WY": 0.6,
}
HIGH_COST_STATES = {"CA", "HI", "MA", "WA", "NY", "NJ", "CO", "DC"}
LOW_COST_STATES = {"MS", "AR", "WV", "OK", "KY", "AL", "IA", "KS", "OH", "IN"}

ZIP_FIRST_DIGIT = {
    **dict.fromkeys(["MA", "RI", "NH", "ME", "VT", "CT", "NJ"], "0"),
    **dict.fromkeys(["NY", "PA", "DE"], "1"),
    **dict.fromkeys(["DC", "MD", "VA", "WV", "NC", "SC"], "2"),
    **dict.fromkeys(["FL", "GA", "AL", "TN", "MS"], "3"),
    **dict.fromkeys(["IN", "KY", "MI", "OH"], "4"),
    **dict.fromkeys(["IA", "MN", "MT", "ND", "SD", "WI"], "5"),
    **dict.fromkeys(["IL", "KS", "MO", "NE"], "6"),
    **dict.fromkeys(["AR", "LA", "OK", "TX"], "7"),
    **dict.fromkeys(["AZ", "CO", "ID", "NM", "NV", "UT", "WY"], "8"),
    **dict.fromkeys(["CA", "OR", "WA", "AK", "HI"], "9"),
}

# ---------------------------------------------------------------- lead mix
CHANNELS = {  # share, conversion multiplier
    "marketplace_listing": (0.55, 1.00),
    "paid_search": (0.15, 0.70),
    "organic_web": (0.12, 0.90),
    "partner_agent": (0.10, 1.60),
    "referral": (0.08, 1.80),
}
CREDIT_BANDS = {  # share, conversion multiplier, rate adjustment (pts)
    "excellent": (0.35, 1.25, -0.25),
    "good": (0.35, 1.05, 0.00),
    "fair": (0.20, 0.80, 0.45),
    "poor": (0.10, 0.45, 1.10),
}

# synthetic 30-year fixed rate curve (piecewise linear), NOT market data
RATE_CURVE = [
    (datetime(2024, 10, 1), 6.30),
    (datetime(2025, 1, 15), 7.00),
    (datetime(2025, 6, 1), 6.80),
    (datetime(2025, 9, 15), 6.30),
    (datetime(2026, 1, 1), 6.20),
    (datetime(2026, 6, 1), 6.40),
    (datetime(2026, 12, 31), 6.10),
    (datetime(2027, 12, 31), 6.00),
]

# ---------------------------------------------------------------- funnel
P_ASSIGN = 0.93
P_PREAPPROVE = 0.18
P_LOCK = {"purchase": 0.32, "refinance": 0.48}
P_FUND = 0.82
P_RELOCK_AFTER_EXPIRY = 0.60
P_REBALANCE = 0.04
P_DUPLICATE = 0.02
P_EXPLICIT_CLOSE = 0.70  # otherwise auto-closed as stale after 90 days
AUTO_CLOSE_DAYS = 90

LOCK_PERIODS = ([30, 45, 60], [0.40, 0.40, 0.20])

LOST_REASONS = {
    "lead_created": (["unreachable", "invalid_contact", "not_interested"], [0.55, 0.20, 0.25]),
    "assigned": (["unresponsive", "not_qualified", "went_with_competitor", "not_ready"], [0.40, 0.20, 0.20, 0.20]),
    "pre_approved": (["no_home_found", "went_with_competitor", "rate_shopping", "not_ready"], [0.40, 0.25, 0.20, 0.15]),
    "rate_locked": (["appraisal_issue", "financing_denied", "withdrawn"], [0.35, 0.30, 0.35]),
}

# ---------------------------------------------------------------- planted stories
# 1) paid search campaign: volume up, quality down
# (start, end, extra paid-search volume, pre-approval multiplier)
PAID_SEARCH_CAMPAIGN = (datetime(2025, 5, 5), datetime(2025, 6, 15), 0.60, 0.50)
# 2) Southeast team attrition crunch: 4 LOs leave, speed-to-assign degrades
SE_CRUNCH_TEAM = 6
SE_CRUNCH_DEPARTURES = (datetime(2026, 2, 2), datetime(2026, 2, 13), 4)
SE_CRUNCH_BACKFILL = (datetime(2026, 3, 2), datetime(2026, 3, 16))
SE_CRUNCH_WINDOW = (datetime(2026, 2, 2), datetime(2026, 2, 28))

FIRST_NAMES = [
    "James",
    "Mary",
    "Robert",
    "Patricia",
    "John",
    "Jennifer",
    "Michael",
    "Linda",
    "David",
    "Elizabeth",
    "William",
    "Barbara",
    "Richard",
    "Susan",
    "Joseph",
    "Jessica",
    "Thomas",
    "Sarah",
    "Carlos",
    "Karen",
    "Daniel",
    "Lisa",
    "Matthew",
    "Nancy",
    "Anthony",
    "Sandra",
    "Mark",
    "Ashley",
    "Luis",
    "Emily",
    "Steven",
    "Kimberly",
    "Andrew",
    "Donna",
    "Wei",
    "Michelle",
    "Kevin",
    "Maria",
    "Brian",
    "Carol",
    "Jose",
    "Amanda",
    "Aisha",
    "Priya",
    "Ryan",
    "Melissa",
    "Jacob",
    "Deborah",
    "Nguyen",
    "Sofia",
    "Omar",
    "Grace",
    "Ethan",
    "Chloe",
    "Diego",
    "Hannah",
    "Jamal",
    "Olivia",
    "Hiroshi",
    "Fatima",
]
LAST_NAMES = [
    "Smith",
    "Johnson",
    "Williams",
    "Brown",
    "Jones",
    "Garcia",
    "Miller",
    "Davis",
    "Rodriguez",
    "Martinez",
    "Hernandez",
    "Lopez",
    "Gonzalez",
    "Wilson",
    "Anderson",
    "Thomas",
    "Taylor",
    "Moore",
    "Jackson",
    "Martin",
    "Lee",
    "Perez",
    "Thompson",
    "White",
    "Harris",
    "Sanchez",
    "Clark",
    "Ramirez",
    "Lewis",
    "Robinson",
    "Walker",
    "Young",
    "Allen",
    "King",
    "Wright",
    "Scott",
    "Torres",
    "Nguyen",
    "Hill",
    "Flores",
    "Green",
    "Adams",
    "Nelson",
    "Baker",
    "Hall",
    "Rivera",
    "Campbell",
    "Mitchell",
    "Carter",
    "Roberts",
    "Patel",
    "Kim",
    "Chen",
    "Shah",
    "Okafor",
    "Cohen",
    "Murphy",
    "Rossi",
    "Tanaka",
    "Dubois",
]
EMAIL_DOMAINS = ["example.com", "example.net", "example.org", "mail.example.com"]
LO_EMAIL_DOMAIN = "lender.example"
