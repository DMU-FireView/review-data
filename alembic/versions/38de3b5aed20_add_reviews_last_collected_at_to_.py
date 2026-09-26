"""add reviews_last_collected_at to products

Revision ID: 38de3b5aed20
Revises: 0d7b992e8261
Create Date: 2026-09-19

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '38de3b5aed20'
down_revision: Union[str, Sequence[str], None] = '0d7b992e8261'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "products",
        sa.Column("reviews_last_collected_at", sa.DateTime(timezone=True), nullable=True),
    )
    # 이미 리뷰를 수집해 둔 상품은 그 시각을 리뷰 수집 시각으로 본다. NULL 로 두면
    # 기존 데이터가 전부 stale 이 되어 배포 직후 재수집 job 이 한꺼번에 쏟아진다.
    op.execute(
        """
        UPDATE products p
        SET reviews_last_collected_at = sub.last_collected_at
        FROM (
            SELECT platform, product_id, max(last_collected_at) AS last_collected_at
            FROM reviews
            GROUP BY platform, product_id
        ) AS sub
        WHERE p.platform = sub.platform AND p.product_id = sub.product_id
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("products", "reviews_last_collected_at")
