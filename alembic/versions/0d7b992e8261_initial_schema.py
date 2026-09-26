"""initial schema

Revision ID: 0d7b992e8261
Revises:
Create Date: 2026-09-03 19:49:27.395698

"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0d7b992e8261'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "products",
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("product_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("brand", sa.Text(), nullable=True),
        sa.Column("manufacturer", sa.Text(), nullable=True),
        sa.Column("seller", sa.Text(), nullable=True),
        sa.Column("price", sa.Integer(), nullable=True),
        sa.Column("thumbnail_url", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("review_count", sa.Integer(), nullable=True),
        sa.Column("rating", sa.Numeric(3, 2), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("first_collected_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_collected_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("platform", "product_id"),
    )

    op.create_table(
        "reviews",
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("product_id", sa.Text(), nullable=False),
        sa.Column("review_id", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("rating", sa.Numeric(3, 2), nullable=True),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("written_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("option", sa.Text(), nullable=True),
        sa.Column("images", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("helpful_count", sa.Integer(), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("first_collected_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_collected_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("platform", "product_id", "review_id"),
        sa.ForeignKeyConstraint(
            ["platform", "product_id"],
            ["products.platform", "products.product_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "idx_reviews_cursor", "reviews", ["platform", "product_id", "written_at"]
    )

    op.create_table(
        "collection_jobs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("product_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("product_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("review_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('pending','running','succeeded','partial','failed')",
            name="ck_collection_jobs_status",
        ),
        sa.CheckConstraint(
            "product_status IN ('pending','succeeded','failed','skipped')",
            name="ck_collection_jobs_product_status",
        ),
        sa.CheckConstraint(
            "review_status IN ('pending','succeeded','failed','skipped')",
            name="ck_collection_jobs_review_status",
        ),
    )
    op.create_index(
        "uq_collection_jobs_inflight",
        "collection_jobs",
        ["platform", "product_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
    op.create_index(
        "idx_collection_jobs_claim", "collection_jobs", ["status", "lease_expires_at"]
    )

    op.create_table(
        "analysis_jobs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("product_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("trigger_collection_job_id", sa.BigInteger(), nullable=True),
        sa.Column("input_review_count", sa.Integer(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('queued','running','done','failed','stale')",
            name="ck_analysis_jobs_status",
        ),
        sa.ForeignKeyConstraint(
            ["platform", "product_id"],
            ["products.platform", "products.product_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["trigger_collection_job_id"], ["collection_jobs.id"]
        ),
    )
    op.create_index(
        "uq_analysis_jobs_inflight",
        "analysis_jobs",
        ["platform", "product_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index(
        "idx_analysis_jobs_claim", "analysis_jobs", ["status", "lease_expires_at"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("analysis_jobs")
    op.drop_table("collection_jobs")
    op.drop_index("idx_reviews_cursor", table_name="reviews")
    op.drop_table("reviews")
    op.drop_table("products")
