"""Create append-only Better records and time series."""

from alembic import op

from better.storage.db import install_immutability, metadata

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    metadata.create_all(op.get_bind())
    install_immutability(op.get_bind())


def downgrade():
    raise RuntimeError("Destructive downgrade disabled: export/archive immutable evidence first")
