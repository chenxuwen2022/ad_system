"""Store common operation descriptions once, preserving target-specific history."""
from alembic import op
import sqlalchemy as sa

revision = "28c9a1e6b504"
down_revision = "7b315c02ad86"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text("""
        UPDATE operation_logs AS log
        SET targets = (
            SELECT jsonb_agg(
                CASE WHEN item.value->>'detail' = log.detail
                     THEN item.value - 'detail' ELSE item.value END
                ORDER BY item.ordinality
            )::json
            FROM jsonb_array_elements(log.targets::jsonb) WITH ORDINALITY AS item(value, ordinality)
        )
        WHERE EXISTS (
            SELECT 1 FROM jsonb_array_elements(log.targets::jsonb) AS item(value)
            WHERE item.value->>'detail' = log.detail
        )
    """))


def downgrade():
    op.execute(sa.text("""
        UPDATE operation_logs AS log
        SET targets = (
            SELECT jsonb_agg(
                CASE WHEN NOT (item.value ? 'detail')
                     THEN item.value || jsonb_build_object('detail', log.detail) ELSE item.value END
                ORDER BY item.ordinality
            )::json
            FROM jsonb_array_elements(log.targets::jsonb) WITH ORDINALITY AS item(value, ordinality)
        )
        WHERE EXISTS (
            SELECT 1 FROM jsonb_array_elements(log.targets::jsonb) AS item(value)
            WHERE NOT (item.value ? 'detail')
        )
    """))
