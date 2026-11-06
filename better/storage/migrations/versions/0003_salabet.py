"""Add append-only SalaBet candidate, slip, selection, event and note records."""

from alembic import op

from better.storage.db import install_immutability, metadata

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    metadata.create_all(op.get_bind())
    install_immutability(op.get_bind())


def downgrade():
    raise RuntimeError("Destructive downgrade disabled: SalaBet history is immutable")
