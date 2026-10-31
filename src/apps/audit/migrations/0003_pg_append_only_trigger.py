from django.db import migrations

CREATE_SQL = """
CREATE OR REPLACE FUNCTION audit_auditlog_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_auditlog is append-only: % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS audit_auditlog_append_only ON audit_auditlog;
CREATE TRIGGER audit_auditlog_append_only
    BEFORE UPDATE OR DELETE ON audit_auditlog
    FOR EACH ROW EXECUTE FUNCTION audit_auditlog_reject_mutation();
"""

DROP_SQL = """
DROP TRIGGER IF EXISTS audit_auditlog_append_only ON audit_auditlog;
DROP FUNCTION IF EXISTS audit_auditlog_reject_mutation();
"""


def install(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(CREATE_SQL)


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(DROP_SQL)


class Migration(migrations.Migration):
    dependencies = [("audit", "0002_alter_auditlog_action_alter_auditlog_request_id_and_more")]
    operations = [migrations.RunPython(install, uninstall)]
