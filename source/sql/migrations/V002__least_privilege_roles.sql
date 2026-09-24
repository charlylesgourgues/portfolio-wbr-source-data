/* V002 - Database roles for least-privilege access.
   Roles only: logins/users are created once by an admin (see README),
   then added with ALTER ROLE <role> ADD MEMBER <user>.                    */

IF DATABASE_PRINCIPAL_ID('ingest_reader') IS NULL CREATE ROLE ingest_reader;
IF DATABASE_PRINCIPAL_ID('generator_writer') IS NULL CREATE ROLE generator_writer;
GO

/* Ingestion tool (Lakeflow Connect / Airbyte / Fivetran): read the CRM only */
GRANT SELECT ON SCHEMA::crm TO ingest_reader;

/* Daily generator (GitHub Actions): upsert CRM, own staging, keep its state */
GRANT SELECT, INSERT, UPDATE, DELETE ON SCHEMA::crm TO generator_writer;
GRANT SELECT, INSERT, UPDATE, DELETE, ALTER ON SCHEMA::staging TO generator_writer;
GRANT SELECT, INSERT, UPDATE, DELETE ON SCHEMA::sim TO generator_writer;
GRANT CREATE TABLE TO generator_writer;
GO
