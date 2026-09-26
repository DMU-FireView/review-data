"""align reviews cursor index with query order

Revision ID: b1cf29089ede
Revises: 38de3b5aed20
Create Date: 2026-09-26

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b1cf29089ede'
down_revision: Union[str, Sequence[str], None] = '38de3b5aed20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 조회는 (written_at DESC NULLS LAST, review_id DESC) 로 정렬하는데 기존 인덱스는
    # written_at 오름차순까지만 담고 tie-breaker 도 없었다. 같은 시각의 리뷰가 많은
    # 상품에서 정렬 비용이 붙으므로 정렬 방향과 NULL 정책을 인덱스에 맞춘다.
    op.drop_index("idx_reviews_cursor", table_name="reviews")
    op.create_index(
        "idx_reviews_cursor",
        "reviews",
        ["platform", "product_id", sa.text("written_at DESC NULLS LAST"), sa.text("review_id DESC")],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("idx_reviews_cursor", table_name="reviews")
    op.create_index("idx_reviews_cursor", "reviews", ["platform", "product_id", "written_at"])
