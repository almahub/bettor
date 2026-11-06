"""Add append-only validation cohort records."""

from alembic import op

from better.storage.db import install_immutability, metadata

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    metadata.create_all(op.get_bind())
    install_immutability(op.get_bind())


def downgrade():
    raise RuntimeError("Destructive downgrade disabled: validation evidence is immutable")
