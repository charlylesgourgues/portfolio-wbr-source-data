-- migrate:no-transaction
/* V003 - Change Tracking on the CRM tables, for Fivetran incremental syncs.
   ALTER DATABASE cannot run inside a transaction, hence the marker above:
   every statement is guarded so the migration can safely be re-run.
   Retention of 7 days covers a daily sync with margin for missed runs.    */

IF NOT EXISTS (SELECT 1 FROM sys.change_tracking_databases WHERE database_id = DB_ID())
    EXEC('ALTER DATABASE CURRENT SET CHANGE_TRACKING = ON (CHANGE_RETENTION = 7 DAYS, AUTO_CLEANUP = ON)');
GO

IF NOT EXISTS (SELECT 1 FROM sys.change_tracking_tables WHERE object_id = OBJECT_ID('crm.teams'))
    ALTER TABLE crm.teams ENABLE CHANGE_TRACKING;
IF NOT EXISTS (SELECT 1 FROM sys.change_tracking_tables WHERE object_id = OBJECT_ID('crm.loan_officers'))
    ALTER TABLE crm.loan_officers ENABLE CHANGE_TRACKING;
IF NOT EXISTS (SELECT 1 FROM sys.change_tracking_tables WHERE object_id = OBJECT_ID('crm.leads'))
    ALTER TABLE crm.leads ENABLE CHANGE_TRACKING;
IF NOT EXISTS (SELECT 1 FROM sys.change_tracking_tables WHERE object_id = OBJECT_ID('crm.lead_assignments'))
    ALTER TABLE crm.lead_assignments ENABLE CHANGE_TRACKING;
IF NOT EXISTS (SELECT 1 FROM sys.change_tracking_tables WHERE object_id = OBJECT_ID('crm.lead_stage_events'))
    ALTER TABLE crm.lead_stage_events ENABLE CHANGE_TRACKING;
IF NOT EXISTS (SELECT 1 FROM sys.change_tracking_tables WHERE object_id = OBJECT_ID('crm.rate_locks'))
    ALTER TABLE crm.rate_locks ENABLE CHANGE_TRACKING;
IF NOT EXISTS (SELECT 1 FROM sys.change_tracking_tables WHERE object_id = OBJECT_ID('crm.fundings'))
    ALTER TABLE crm.fundings ENABLE CHANGE_TRACKING;
GO

/* What Fivetran needs on top of SELECT (granted in V002) */
GRANT VIEW DEFINITION ON SCHEMA::crm TO ingest_reader;
GRANT VIEW CHANGE TRACKING ON SCHEMA::crm TO ingest_reader;
GO
