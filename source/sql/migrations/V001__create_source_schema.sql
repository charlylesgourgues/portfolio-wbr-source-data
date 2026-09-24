/* V001 - Source operational database (CRM / LOS simulation), Azure SQL.
   Applied by source/migrate.py, which records it in sim.schema_migrations.
   Never edit an applied migration: add V002__..., V003__... instead.
   Schemas: crm = simulated operational tables, staging = transient MERGE
   tables, sim = generator / migration bookkeeping. Timestamps are UTC.     */

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'crm')     EXEC('CREATE SCHEMA crm');
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'staging') EXEC('CREATE SCHEMA staging');
GO

/* ---------------------------------------------------------------- teams */
CREATE TABLE crm.teams (
    team_id        INT            NOT NULL PRIMARY KEY,
    team_name      NVARCHAR(100)  NOT NULL,
    region         NVARCHAR(50)   NOT NULL,
    covered_states NVARCHAR(200)  NOT NULL,   -- comma-separated state codes
    updated_at     DATETIME2(0)   NOT NULL
);

/* -------------------------------------------------------- loan_officers */
/* Current state only: a team transfer overwrites team_id.
   History is rebuilt downstream with a dbt snapshot (SCD2).              */
CREATE TABLE crm.loan_officers (
    lo_id            INT            NOT NULL PRIMARY KEY,
    first_name       NVARCHAR(100)  NOT NULL,
    last_name        NVARCHAR(100)  NOT NULL,
    email            NVARCHAR(200)  NOT NULL,
    team_id          INT            NOT NULL REFERENCES crm.teams(team_id),
    hire_date        DATE           NOT NULL,
    termination_date DATE           NULL,
    is_active        BIT            NOT NULL,
    updated_at       DATETIME2(0)   NOT NULL
);
CREATE INDEX ix_lo_updated_at ON crm.loan_officers(updated_at);

/* ---------------------------------------------------------------- leads */
/* Current state of each lead, as the CRM shows it.
   Contact fields are deliberately messy (casing, phone formats, dupes).  */
CREATE TABLE crm.leads (
    lead_id          INT            NOT NULL PRIMARY KEY,
    created_at       DATETIME2(0)   NOT NULL,
    first_name       NVARCHAR(100)  NULL,
    last_name        NVARCHAR(100)  NULL,
    email            NVARCHAR(200)  NULL,
    phone            NVARCHAR(40)   NULL,
    channel          NVARCHAR(50)   NOT NULL,  -- marketplace_listing, paid_search, organic_web, partner_agent, referral
    loan_purpose     NVARCHAR(20)   NOT NULL,  -- purchase, refinance
    property_state   CHAR(2)        NOT NULL,
    property_zip     VARCHAR(10)    NULL,
    est_loan_amount  DECIMAL(12,2)  NULL,
    credit_band      NVARCHAR(20)   NULL,      -- excellent, good, fair, poor
    current_stage    NVARCHAR(30)   NOT NULL,  -- lead_created, assigned, pre_approved, rate_locked, funded
    status           NVARCHAR(20)   NOT NULL,  -- open, funded, closed_lost
    lost_reason      NVARCHAR(50)   NULL,
    assigned_lo_id   INT            NULL REFERENCES crm.loan_officers(lo_id),
    closed_at        DATETIME2(0)   NULL,
    updated_at       DATETIME2(0)   NOT NULL
);
CREATE INDEX ix_leads_updated_at ON crm.leads(updated_at);

/* ------------------------------------------------------ lead_assignments */
CREATE TABLE crm.lead_assignments (
    assignment_id     INT           NOT NULL PRIMARY KEY,
    lead_id           INT           NOT NULL REFERENCES crm.leads(lead_id),
    lo_id             INT           NOT NULL REFERENCES crm.loan_officers(lo_id),
    assigned_at       DATETIME2(0)  NOT NULL,
    unassigned_at     DATETIME2(0)  NULL,
    assignment_reason NVARCHAR(30)  NOT NULL,  -- initial, rebalance, lo_departure
    updated_at        DATETIME2(0)  NOT NULL
);
CREATE INDEX ix_assign_updated_at ON crm.lead_assignments(updated_at);

/* ----------------------------------------------------- lead_stage_events */
/* Append-only event log.
   event_ts    = when it happened in the business
   recorded_at = when the source system wrote it (LOS events can be late,
                 and are occasionally double-posted).
   stage also includes 'closed_lost'.                                     */
CREATE TABLE crm.lead_stage_events (
    event_id      INT           NOT NULL PRIMARY KEY,
    lead_id       INT           NOT NULL REFERENCES crm.leads(lead_id),
    stage         NVARCHAR(30)  NOT NULL,
    event_ts      DATETIME2(0)  NOT NULL,
    lo_id         INT           NULL REFERENCES crm.loan_officers(lo_id),
    source_system NVARCHAR(10)  NOT NULL,      -- crm, los
    recorded_at   DATETIME2(0)  NOT NULL
);
CREATE INDEX ix_events_recorded_at ON crm.lead_stage_events(recorded_at);

/* ------------------------------------------------------------ rate_locks */
CREATE TABLE crm.rate_locks (
    lock_id           INT           NOT NULL PRIMARY KEY,
    lead_id           INT           NOT NULL REFERENCES crm.leads(lead_id),
    locked_at         DATETIME2(0)  NOT NULL,
    lock_period_days  INT           NOT NULL,
    interest_rate     DECIMAL(5,3)  NOT NULL,
    loan_amount       DECIMAL(12,2) NOT NULL,
    expires_at        DATETIME2(0)  NOT NULL,  -- moves forward when extended
    extension_days    INT           NOT NULL,
    status            NVARCHAR(20)  NOT NULL,  -- active, extended, funded, expired, cancelled
    updated_at        DATETIME2(0)  NOT NULL
);
CREATE INDEX ix_locks_updated_at ON crm.rate_locks(updated_at);

/* -------------------------------------------------------------- fundings */
CREATE TABLE crm.fundings (
    funding_id     INT           NOT NULL PRIMARY KEY,
    lead_id        INT           NOT NULL REFERENCES crm.leads(lead_id),
    lock_id        INT           NOT NULL REFERENCES crm.rate_locks(lock_id),
    funded_at      DATETIME2(0)  NOT NULL,
    funded_amount  DECIMAL(12,2) NOT NULL,
    recorded_at    DATETIME2(0)  NOT NULL
);
CREATE INDEX ix_fundings_recorded_at ON crm.fundings(recorded_at);

/* ------------------------------------------------ generator bookkeeping */
CREATE TABLE sim.generator_state (
    id          INT          NOT NULL PRIMARY KEY,
    seed        INT          NOT NULL,
    last_as_of  DATETIME2(0) NOT NULL,
    updated_at  DATETIME2(0) NOT NULL
);
GO
