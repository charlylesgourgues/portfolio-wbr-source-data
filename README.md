# WBR portfolio: Weekly Business Review, end to end

This project rebuilds a mortgage lender's Weekly Business Review on a modern data stack:

```
Azure SQL (simulated CRM/LOS) ─► Databricks bronze ─► dbt silver/gold (star schema) ─► Power BI
                                                                  └─► UC metric views ─► Genie
```

| Folder | Status | What it is |
|---|---|---|
| `source/` | ✅ phase 1 | Source database schema (versioned migrations) and the synthetic data generator |
| `ingestion/`, `dbt/`, `powerbi/`, `genie/` | planned | Next steps of phase 1 and phase 2 |

All data is synthetic. Names, emails (`example.*` domains), rates and volumes are invented, and calibrated only to be plausible.

## CI/CD

```
 feature branch ──PR──►  CI  ─────────────────────────────────►  merge to main ──►  CD
                         ├─ lint: ruff, format, migration rules        (protected)     └─ apply new migrations
                         ├─ data contract tests (pytest, 18 tests)                       to Azure SQL
                         └─ integration: migrations + full load +                        (environment "production")
                            2 MERGE increments on a SQL Server 2022
                            container, checked against the expected snapshot
 daily 07:17 UTC ─► refuses to run if migrations are pending ─► upsert yesterday's changes
```

| Workflow | Trigger | Does |
|---|---|---|
| `ci.yml` | every PR and push to `main` touching `source/` | Lint and SQL rules, data contract tests, end-to-end integration test on a throwaway SQL Server |
| `deploy.yml` | merge to `main` touching migrations, or manual | `migrate.py apply` against Azure SQL, with admin credentials, in the `production` environment |
| `daily-increment.yml` | every day, or manual (`increment` / `init`) | Generator load, with least-privilege credentials |

Deployment and daily load share a concurrency group, so they never write to the database at the same time.

**Schema changes** go through `source/sql/migrations/V<nnn>__<name>.sql` files, never edited once applied. `migrate.py` records each file's checksum in `sim.schema_migrations`, and refuses to continue if an applied file was modified. CI rejects `DROP TABLE` in migrations.

**What the tests check** (`source/generator/tests`):
- keys and foreign keys,
- no rows from the future,
- `full(T) + increment(T→T+n) == full(T+n)`,
- funnel rates within expected bands,
- the data-quality defects and the planted stories are present,
- same seed ⇒ identical data.

## One-time setup

1. **GitHub**
   - Push the repo.
   - In *Settings → Branches*, protect `main`: require a PR, and require the `Lint + SQL validation`, `Data contract tests` and `Migrations + load on SQL Server 2022` checks.
   - In *Settings → Environments*, create `production`. You can add yourself as a required reviewer, to get a manual approval before each deployment.
2. **Secrets** (environment `production`)
   - `AZURE_SQL_ADMIN_CONN`: ODBC connection string for an admin, used only by CD.
   - `AZURE_SQL_CONN`: connection string for the `wbr_generator` user created in step 4.

   Format: `Driver={ODBC Driver 18 for SQL Server};Server=tcp:<server>.database.windows.net,1433;Database=<db>;Uid=<user>;Pwd=<password>;Encrypt=yes;TrustServerCertificate=no;Connection Timeout=60;`
3. **Azure SQL firewall:** GitHub-hosted runners have changing IPs, so allow them with a wide rule. Credentials stay least-privilege.
4. **First deploy:** run *CD - deploy source schema* manually. This creates the `crm`, `staging` and `sim` schemas and the roles. Then, as admin:
   ```sql
   -- in master
   CREATE LOGIN wbr_generator WITH PASSWORD = '<strong password>';
   CREATE LOGIN wbr_ingest    WITH PASSWORD = '<strong password>';
   -- in the project database
   CREATE USER wbr_generator FOR LOGIN wbr_generator;  ALTER ROLE generator_writer ADD MEMBER wbr_generator;
   CREATE USER wbr_ingest    FOR LOGIN wbr_ingest;     ALTER ROLE ingest_reader    ADD MEMBER wbr_ingest;
   ```
5. **Initial load:** run *Source data load (daily)* manually with `mode = init`. After that, the schedule takes over. GitHub pauses schedules after 60 days without repo activity; re-enable the workflow from the Actions tab if that happens.

## Local development

Run these from the repo root. To write to a database, you also need [ODBC Driver 18 for SQL Server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server).

**Windows (PowerShell)**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1                      # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r source/requirements-dev.txt

# same checks as CI (each step runs only if the previous one passed)
ruff check .; if ($?) { ruff format --check . }; if ($?) { python source/migrate.py validate }; if ($?) { pytest -q }

cd source/generator
python generate.py stats                          # funnel summary, no writes
python generate.py init --target csv --out data   # CSV instead of a database

# target a database: connection string for the current session only
$env:AZURE_SQL_CONN = "Driver={ODBC Driver 18 for SQL Server};Server=tcp:<server>.database.windows.net,1433;Database=<db>;Uid=<user>;Pwd=<password>;Encrypt=yes;TrustServerCertificate=no;Connection Timeout=60;"
python generate.py increment --target azure
```

**macOS / Linux (bash)**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r source/requirements-dev.txt
ruff check . && ruff format --check . && python source/migrate.py validate && pytest -q
cd source/generator
python generate.py stats
python generate.py init --target csv --out data
export AZURE_SQL_CONN="Driver={ODBC Driver 18 for SQL Server};Server=tcp:...;"
python generate.py increment --target azure
```

To fix lint and formatting issues automatically: `ruff check --fix .` then `ruff format .`.

## Source simulator

About 70 leads a day across 51 US jurisdictions, starting on 2024-10-01. Each lead moves through a 5-stage funnel:

`lead_created → assigned → pre_approved → rate_locked → funded`

| Table (`crm` schema) | Grain | Changes over time |
|---|---|---|
| `teams` | sales team (7, by region) | static |
| `loan_officers` | LO, **current state only** | hires, terminations, team transfers (→ SCD2 via dbt snapshot) |
| `leads` | lead, current state | stage, status, owner, `updated_at` |
| `lead_assignments` | lead × LO ownership interval | reassignments (rebalance, LO departure) |
| `lead_stage_events` | append-only event log | `event_ts` (business time) vs `recorded_at` (system time) |
| `rate_locks` | lock | extensions, expiry, re-locks, cancellations |
| `fundings` | funded loan | append-only |

Mature-cohort funnel with seed 42: 91% assigned, 18% pre-approved, 7% locked, **~6% funded**. Conversion varies by channel (referral ~13%, paid search ~3%), credit band and LO skill. The rate curve is synthetic, and it drives the refinance share and lock rates.

### Deliberate data-quality problems (for the silver layer to fix)
- About 2% duplicate leads: same person, resubmitted within 72h, with different email casing or phone format. Most are closed as `duplicate`, but not all.
- Late-arriving LOS events: about 8% arrive minutes to hours late, and 1.5% arrive 2–10 days late. So `recorded_at` order ≠ `event_ts` order.
- A few LOS events are double-posted (same lead, stage and timestamp, but a new `event_id`).
- Inconsistent phone formats, uppercase emails and trailing spaces, plus some missing `email`, `phone`, `est_loan_amount` and `credit_band` values.
- About 30% of dead leads are never closed by the LO and get auto-closed at 90 days (`stale_auto_closed`).

### Planted stories (for WBR commentary and the Genie benchmark)
1. **Paid search campaign, 2025-05-05 → 2025-06-14:** paid-search volume goes up ~70%, and its pre-approval rate drops from ~13% to ~5%.
2. **Southeast team crunch, February 2026:** 4 LOs leave, and median speed-to-assign goes from ~10 min to ~4 h. The assignment rate falls from 91% to 83%, and recovers after the March backfills.

### How the generator works
`universe.py` simulates every lead's **full lifecycle up to 2027-12-31** from a seed. `snapshot.py` then cuts the tables as they looked *as of* a timestamp:
- `init` = the full state as of now,
- `increment` = only the rows whose `updated_at` / `recorded_at` falls after the last run (read from `sim.generator_state`), applied with `MERGE`.

A missed day is caught up by the next run, and a second run on the same day only touches what changed in between.

## Roadmap: CI/CD for the next layers
- **dbt:** on each PR, `dbt build` in a per-PR schema on Databricks (*slim CI*: `state:modified+` against the production manifest), plus `sqlfluff`. On merge, deploy the project as a Databricks job with **Databricks Asset Bundles** (`databricks bundle deploy`).
- **Power BI:** store the report in **PBIP** format, which is Git-friendly (TMDL model + report JSON). CI runs Best Practice Analyzer rules on the model. Automated publishing to the Power BI service needs a workspace and a service principal, so it's optional.
- **Genie:** keep the metric view YAML and the benchmark questions in the repo, and have CI re-run the accuracy benchmark.
