"""
워커 신뢰성 통합 테스트 (issue #14).

여기서 검증하는 것은 "정상 경로가 동작한다"가 아니라, 워커가 죽거나 느려질 때
job 이 어떻게 되는가다. conftest 의 engine fixture 가 로컬 Postgres 접속을 시도하며,
접속 실패 시 이 파일의 테스트는 모두 skip 된다.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from review_data.core.base import BaseCollector
from review_data.core.db.repository import (
    CollectionJobRepository,
    ProductRepository,
)
from review_data.core.exceptions import ParseError
from review_data.core.models import Product, Review
from review_data.worker import collection_worker

PLATFORM = "relyplat"


async def _new_job(session_factory, product_id: str) -> int:
    async with session_factory() as s:
        job, _ = await CollectionJobRepository(s).create_or_get_active(PLATFORM, product_id)
        job_id = job.id
        await s.commit()
    return job_id


async def _expire_lease(session_factory, job_id: int) -> None:
    """워커가 응답 없이 죽어 lease 만 남은 상황을 만든다."""
    async with session_factory() as s:
        job = await CollectionJobRepository(s).get(job_id)
        job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()


async def test_completion_rejected_after_lease_lost(session_factory):
    """lease 를 잃은 워커가 뒤늦게 끝나도 새 워커의 상태를 덮어쓰면 안 된다."""
    job_id = await _new_job(session_factory, "lost-1")

    async with session_factory() as s:
        claimed = await CollectionJobRepository(s).claim_one("worker-a")
        assert claimed.id == job_id
        await s.commit()

    await _expire_lease(session_factory, job_id)

    async with session_factory() as s:
        stolen = await CollectionJobRepository(s).claim_one("worker-b")
        assert stolen is not None
        assert stolen.locked_by == "worker-b"
        await s.commit()

    async with session_factory() as s:
        owned = await CollectionJobRepository(s).mark_completed(
            job_id,
            worker_id="worker-a",
            product_status="succeeded",
            review_status="succeeded",
        )
        await s.commit()
    assert owned is False

    async with session_factory() as s:
        row = await CollectionJobRepository(s).get(job_id)
        # worker-b 가 아직 들고 있으므로 running 이 유지되어야 한다.
        assert row.status == "running"
        assert row.locked_by == "worker-b"


async def test_renew_lease_fails_once_ownership_lost(session_factory):
    job_id = await _new_job(session_factory, "renew-1")

    async with session_factory() as s:
        await CollectionJobRepository(s).claim_one("worker-a")
        await s.commit()

    async with session_factory() as s:
        assert await CollectionJobRepository(s).renew_lease(job_id, "worker-a") is True
        await s.commit()

    await _expire_lease(session_factory, job_id)
    async with session_factory() as s:
        await CollectionJobRepository(s).claim_one("worker-b")
        await s.commit()

    async with session_factory() as s:
        assert await CollectionJobRepository(s).renew_lease(job_id, "worker-a") is False


async def test_claim_skips_job_that_exhausted_attempts(session_factory):
    """워커를 계속 죽이는 job 이 큐를 영원히 막지 않아야 한다."""
    job_id = await _new_job(session_factory, "exhaust-1")

    async with session_factory() as s:
        job = await CollectionJobRepository(s).get(job_id)
        job.status = "running"
        job.locked_by = "dead-worker"
        job.attempt_count = job.max_attempts
        job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()

    async with session_factory() as s:
        assert await CollectionJobRepository(s).claim_one("worker-new") is None


async def test_fail_exhausted_cleans_up_zombie_job(session_factory):
    job_id = await _new_job(session_factory, "exhaust-2")

    async with session_factory() as s:
        job = await CollectionJobRepository(s).get(job_id)
        job.status = "running"
        job.locked_by = "dead-worker"
        job.attempt_count = job.max_attempts
        job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()

    async with session_factory() as s:
        reaped = await CollectionJobRepository(s).fail_exhausted()
        await s.commit()
    assert reaped == 1

    async with session_factory() as s:
        row = await CollectionJobRepository(s).get(job_id)
        assert row.status == "failed"
        assert "최대 시도 횟수 초과" in row.last_error


class _SlowCollector(BaseCollector):
    """리뷰 수집이 lease 보다 오래 걸리는 collector."""

    platform = PLATFORM
    review_delay = 3.0

    async def search_products(self, keyword: str, limit: int = 20) -> list[Product]:
        return []

    async def get_product(self, product_id: str) -> Product:
        return Product(platform=self.platform, product_id=product_id, name="느린상품", url="https://x")

    async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
        await asyncio.sleep(self.review_delay)
        return [
            Review(platform=self.platform, product_id=product_id, review_id="r1", content="리뷰")
        ]


async def test_heartbeat_keeps_lease_alive_during_long_collection(
    session_factory, monkeypatch
):
    """수집이 lease 보다 길어도 heartbeat 가 lease 를 연장해 job 을 지켜야 한다."""
    monkeypatch.setattr(
        collection_worker, "discover", lambda: ({PLATFORM: _SlowCollector}, [])
    )
    job_id = await _new_job(session_factory, "slow-1")

    async def steal_attempt():
        # lease(2초) 가 heartbeat 없이는 이미 만료됐을 시점에 가로채기를 시도한다.
        await asyncio.sleep(2.5)
        async with session_factory() as s:
            stolen = await CollectionJobRepository(s).claim_one("worker-thief")
            await s.commit()
        return stolen

    worker_task = collection_worker.run_once(
        session_factory, "worker-slow", lease_seconds=2
    )
    processed, stolen = await asyncio.gather(worker_task, steal_attempt())

    assert processed is True
    assert stolen is None, "heartbeat 가 lease 를 연장하지 못해 job 을 빼앗겼습니다."

    async with session_factory() as s:
        row = await CollectionJobRepository(s).get(job_id)
        assert row.status == "succeeded"
        assert row.locked_by == "worker-slow"


class _ConcurrentWriteCollector(BaseCollector):
    """수집 도중 별도 세션으로 같은 상품 row 를 건드리는 collector.

    워커가 수집 내내 트랜잭션을 들고 있으면 이 쓰기가 row lock 에 걸려 멈춘다.
    """

    platform = PLATFORM
    session_factory = None

    async def search_products(self, keyword: str, limit: int = 20) -> list[Product]:
        return []

    async def get_product(self, product_id: str) -> Product:
        return Product(platform=self.platform, product_id=product_id, name="동시쓰기", url="https://x")

    async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
        async with type(self).session_factory() as s:
            await ProductRepository(s).upsert(
                Product(
                    platform=self.platform,
                    product_id=product_id,
                    name="외부에서 먼저 씀",
                    url="https://outside",
                )
            )
            await s.commit()
        return []


async def test_worker_holds_no_db_transaction_during_collection(
    session_factory, monkeypatch
):
    """수집 중에는 DB 트랜잭션을 들고 있지 않아야 한다.

    들고 있으면 같은 row 를 건드리는 다른 세션이 lock 대기에 빠져 타임아웃 난다.
    """
    _ConcurrentWriteCollector.session_factory = session_factory
    monkeypatch.setattr(
        collection_worker, "discover", lambda: ({PLATFORM: _ConcurrentWriteCollector}, [])
    )
    await _new_job(session_factory, "concurrent-1")

    try:
        processed = await asyncio.wait_for(
            collection_worker.run_once(session_factory, "worker-c"), timeout=10
        )
    except TimeoutError:
        pytest.fail("수집 중 DB 트랜잭션을 들고 있어 외부 쓰기가 lock 대기에 걸렸습니다.")

    assert processed is True


class _EmptyProductCollector(BaseCollector):
    """필수 정보를 찾지 못해 ParseError 를 던지는 collector (없는 상품)."""

    platform = PLATFORM

    async def search_products(self, keyword: str, limit: int = 20) -> list[Product]:
        return []

    async def get_product(self, product_id: str) -> Product:
        raise ParseError(f"[{self.platform}] 상품 정보를 찾지 못했습니다.")

    async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
        raise ParseError(f"[{self.platform}] 리뷰를 찾지 못했습니다.")


async def test_missing_product_is_recorded_as_failed(session_factory, monkeypatch):
    """없는 상품 수집은 성공으로 기록되면 안 되고, 빈 row 도 남지 않아야 한다."""
    monkeypatch.setattr(
        collection_worker, "discover", lambda: ({PLATFORM: _EmptyProductCollector}, [])
    )
    job_id = await _new_job(session_factory, "missing-1")

    await collection_worker.run_once(session_factory, "worker-missing")

    async with session_factory() as s:
        row = await CollectionJobRepository(s).get(job_id)
        assert row.status == "failed"
        assert row.product_status == "failed"
        # 이름 없는 상품이 저장되어 있으면 안 된다.
        assert await ProductRepository(s).get(PLATFORM, "missing-1") is None
