"""
상품/리뷰 신선도 분리 테스트 (issue #16).

핵심은 "리뷰 수집에 실패한 상품이 신선한 것으로 굳지 않는다"이다. 굳어버리면
TTL 이 끝날 때까지 리뷰 없이 고정되고 재수집 job 도 만들어지지 않는다.
"""

from datetime import UTC, datetime, timedelta

from review_data.core.base import BaseCollector
from review_data.core.db.repository import (
    CollectionJobRepository,
    ProductRepository,
)
from review_data.core.exceptions import CollectorError
from review_data.core.models import Product, Review
from review_data.core.service.collection import CollectionService
from review_data.worker import collection_worker

PLATFORM = "freshplat"


class _ReviewFailsCollector(BaseCollector):
    """상품은 되지만 리뷰가 실패하는 collector (429 등)."""

    platform = PLATFORM

    async def search_products(self, keyword: str, limit: int = 20) -> list[Product]:
        return []

    async def get_product(self, product_id: str) -> Product:
        return Product(platform=self.platform, product_id=product_id, name="상품", url="https://x")

    async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
        raise CollectorError("리뷰 수집 실패")


class _NoReviewsCollector(BaseCollector):
    """리뷰 수집은 성공했지만 실제로 리뷰가 0건인 상품."""

    platform = PLATFORM

    async def search_products(self, keyword: str, limit: int = 20) -> list[Product]:
        return []

    async def get_product(self, product_id: str) -> Product:
        return Product(platform=self.platform, product_id=product_id, name="신상품", url="https://x")

    async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
        return []


async def _run_worker_for(session_factory, monkeypatch, collector_cls, product_id: str) -> None:
    monkeypatch.setattr(
        collection_worker, "discover", lambda: ({PLATFORM: collector_cls}, [])
    )
    async with session_factory() as s:
        await CollectionJobRepository(s).create_or_get_active(PLATFORM, product_id)
        await s.commit()
    await collection_worker.run_once(session_factory, "worker-fresh")


async def test_review_failure_does_not_freeze_product_as_fresh(
    session_factory, monkeypatch
):
    """리뷰 수집이 실패하면 다음 조회에서 stale + 재수집 job 이 나와야 한다."""
    await _run_worker_for(session_factory, monkeypatch, _ReviewFailsCollector, "partial-1")

    async with session_factory() as s:
        job = await CollectionJobRepository(s).get_active(PLATFORM, "partial-1")
        assert job is None, "이전 job 은 이미 끝나 있어야 한다"

        result = await CollectionService(s).get_or_queue(PLATFORM, "partial-1")
        await s.commit()

    assert result.status == "stale", "리뷰를 못 가져왔는데 fresh 로 굳었습니다."
    assert result.product is not None, "마지막 정상 상품 데이터는 그대로 반환해야 한다"
    assert result.job is not None, "재수집 job 이 만들어져야 한다"


async def test_product_without_reviews_stays_fresh(session_factory, monkeypatch):
    """리뷰가 실제로 0건인 상품은 매번 job 을 만들지 않아야 한다."""
    await _run_worker_for(session_factory, monkeypatch, _NoReviewsCollector, "empty-1")

    async with session_factory() as s:
        result = await CollectionService(s).get_or_queue(PLATFORM, "empty-1")
        await s.commit()

    assert result.status == "fresh"
    assert result.reviews == []
    assert result.job is None


async def test_stale_when_reviews_expired_though_product_fresh(session_factory):
    """상품은 아직 신선해도 리뷰가 오래됐으면 재수집 대상이다."""
    async with session_factory() as s:
        repo = ProductRepository(session=s)
        await repo.upsert(
            Product(platform=PLATFORM, product_id="expired-1", name="상품", url="https://x")
        )
        await repo.mark_reviews_collected(PLATFORM, "expired-1")
        row = await repo.get(PLATFORM, "expired-1")
        # 상품은 방금 수집, 리뷰만 오래된 상태로 만든다.
        row.reviews_last_collected_at = datetime.now(UTC) - timedelta(days=7)
        await s.commit()

    async with session_factory() as s:
        result = await CollectionService(s).get_or_queue(PLATFORM, "expired-1")
        await s.commit()

    assert result.status == "stale"
    assert result.job is not None


async def test_successful_collection_marks_both_fresh(session_factory, monkeypatch):
    async with session_factory() as s:
        before = await ProductRepository(s).get(PLATFORM, "ok-1")
    assert before is None

    class _OkCollector(BaseCollector):
        platform = PLATFORM

        async def search_products(self, keyword: str, limit: int = 20) -> list[Product]:
            return []

        async def get_product(self, product_id: str) -> Product:
            return Product(
                platform=self.platform, product_id=product_id, name="정상", url="https://x"
            )

        async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
            return [
                Review(
                    platform=self.platform,
                    product_id=product_id,
                    review_id="r1",
                    content="좋아요",
                )
            ]

    await _run_worker_for(session_factory, monkeypatch, _OkCollector, "ok-1")

    async with session_factory() as s:
        row = await ProductRepository(s).get(PLATFORM, "ok-1")
        assert row.reviews_last_collected_at is not None
        result = await CollectionService(s).get_or_queue(PLATFORM, "ok-1")
        await s.commit()

    assert result.status == "fresh"
    assert len(result.reviews) == 1
