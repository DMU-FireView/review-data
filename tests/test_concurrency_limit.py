"""
플랫폼별 동시 실행 상한과 수집 타임아웃 테스트 (issue #20).

워커 프로세스 하나는 job 을 순차 처리하므로, 상한은 워커를 여러 개 띄웠을 때
의미가 있다. 여기서는 여러 워커가 claim 하는 상황을 직접 흉내 낸다.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from review_data.core.base import BaseCollector
from review_data.core.browser import BrowserCollector
from review_data.core.db.repository import CollectionJobRepository
from review_data.core.models import Product, Review
from review_data.core.settings import Settings
from review_data.worker import collection_worker

BROWSER_PLATFORM = "browserplat"
HTTP_PLATFORM = "httpplat"


async def _new_job(session_factory, platform: str, product_id: str) -> int:
    async with session_factory() as s:
        job, _ = await CollectionJobRepository(s).create_or_get_active(platform, product_id)
        job_id = job.id
        await s.commit()
    return job_id


async def test_claim_respects_platform_cap(session_factory):
    """상한에 도달한 플랫폼의 job 은 더 집어가지 않아야 한다."""
    await _new_job(session_factory, BROWSER_PLATFORM, "b-1")
    await _new_job(session_factory, BROWSER_PLATFORM, "b-2")

    caps = {BROWSER_PLATFORM: 1}

    async with session_factory() as s:
        first = await CollectionJobRepository(s).claim_one(
            "worker-1", platform_caps=caps, default_cap=4
        )
        await s.commit()
    assert first is not None, "첫 job 은 집어갈 수 있어야 한다"

    async with session_factory() as s:
        second = await CollectionJobRepository(s).claim_one(
            "worker-2", platform_caps=caps, default_cap=4
        )
        await s.commit()
    assert second is None, "상한이 1인데 두 번째 job 까지 집어갔습니다."


async def test_capped_platform_does_not_block_other_platforms(session_factory):
    """한 플랫폼이 상한에 걸려도 다른 플랫폼 job 은 계속 처리되어야 한다."""
    await _new_job(session_factory, BROWSER_PLATFORM, "b-1")
    await _new_job(session_factory, BROWSER_PLATFORM, "b-2")
    await _new_job(session_factory, HTTP_PLATFORM, "h-1")

    caps = {BROWSER_PLATFORM: 1, HTTP_PLATFORM: 4}

    claimed_platforms = []
    for worker in ("w1", "w2", "w3"):
        async with session_factory() as s:
            job = await CollectionJobRepository(s).claim_one(
                worker, platform_caps=caps, default_cap=4
            )
            await s.commit()
        if job is not None:
            claimed_platforms.append(job.platform)

    assert claimed_platforms.count(BROWSER_PLATFORM) == 1
    assert claimed_platforms.count(HTTP_PLATFORM) == 1


async def test_expired_lease_does_not_count_toward_cap(session_factory):
    """죽은 워커가 남긴 job 이 상한을 잡아먹어 큐를 막으면 안 된다."""
    job_id = await _new_job(session_factory, BROWSER_PLATFORM, "b-1")

    async with session_factory() as s:
        repo = CollectionJobRepository(s)
        await repo.claim_one("dead-worker", platform_caps={BROWSER_PLATFORM: 1})
        job = await repo.get(job_id)
        job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()

    async with session_factory() as s:
        reclaimed = await CollectionJobRepository(s).claim_one(
            "worker-new", platform_caps={BROWSER_PLATFORM: 1}
        )
        await s.commit()

    assert reclaimed is not None, "lease 만료된 job 이 상한을 계속 점유했습니다."
    assert reclaimed.locked_by == "worker-new"


class _HangingCollector(BaseCollector):
    """리뷰 수집이 끝나지 않는 collector (멈춘 브라우저 페이지를 흉내)."""

    platform = HTTP_PLATFORM

    async def search_products(self, keyword: str, limit: int = 20) -> list[Product]:
        return []

    async def get_product(self, product_id: str) -> Product:
        return Product(platform=self.platform, product_id=product_id, name="상품", url="https://x")

    async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
        await asyncio.sleep(3600)
        return []


async def test_collect_timeout_fails_job_without_killing_worker(
    session_factory, monkeypatch
):
    monkeypatch.setattr(
        collection_worker, "discover", lambda: ({HTTP_PLATFORM: _HangingCollector}, [])
    )
    job_id = await _new_job(session_factory, HTTP_PLATFORM, "hang-1")

    settings = Settings(collect_timeout_seconds=1.0)
    processed = await asyncio.wait_for(
        collection_worker.run_once(session_factory, "worker-t", settings=settings),
        timeout=20,
    )

    assert processed is True, "워커는 타임아웃 후에도 정상 반환해야 한다"

    async with session_factory() as s:
        row = await CollectionJobRepository(s).get(job_id)
    assert row.status == "failed"
    assert "시간 초과" in row.last_error


def test_browser_collector_gets_longer_timeout():
    """플랫폼 이름이 아니라 collector 클래스로 브라우저 여부를 판별해야 한다."""

    class _Browser(BrowserCollector):
        platform = BROWSER_PLATFORM

        async def search_products(self, keyword: str, limit: int = 20) -> list[Product]:
            return []

        async def get_product(self, product_id: str) -> Product:
            raise NotImplementedError

        async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
            raise NotImplementedError

    settings = Settings(collect_timeout_seconds=10.0, browser_collect_timeout_seconds=99.0)

    assert collection_worker._collect_timeout(_Browser, settings) == 99.0
    assert collection_worker._collect_timeout(_HangingCollector, settings) == 10.0

    caps = collection_worker._platform_caps(
        {BROWSER_PLATFORM: _Browser, HTTP_PLATFORM: _HangingCollector},
        Settings(
            max_concurrent_jobs_per_platform=4,
            max_concurrent_browser_jobs_per_platform=1,
        ),
    )
    assert caps == {BROWSER_PLATFORM: 1, HTTP_PLATFORM: 4}
