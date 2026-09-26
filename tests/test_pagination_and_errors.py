"""
커서 페이지네이션과 오류 응답 계약 테스트 (issue #18).

페이지네이션은 구현에서 가장 까다로운 부분인데 그동안 테스트가 없었다. 여기서는
동률(written_at 이 같은 리뷰), NULL, 변조된 cursor 까지 함께 확인한다.
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from review_data.api.app import app
from review_data.core.db.repository import (
    InvalidCursorError,
    ProductRepository,
    ReviewRepository,
    _encode_review_cursor,
)
from review_data.core.models import Product, Review

PLATFORM = "pageplat"
PRODUCT_ID = "page-1"

# written_at 내림차순(NULL 은 마지막), 같으면 review_id 내림차순이 기대 순서다.
_SEED = [
    ("r1", datetime(2026, 1, 5, tzinfo=UTC)),
    ("r2", datetime(2026, 1, 4, tzinfo=UTC)),
    ("r3", datetime(2026, 1, 4, tzinfo=UTC)),  # r2 와 동률
    ("r4", datetime(2026, 1, 3, tzinfo=UTC)),
    ("r5", None),
    ("r6", None),
]
_EXPECTED_ORDER = ["r1", "r3", "r2", "r4", "r6", "r5"]


@pytest.fixture
async def seeded(session):
    await ProductRepository(session).upsert(
        Product(platform=PLATFORM, product_id=PRODUCT_ID, name="상품", url="https://x")
    )
    await ReviewRepository(session).upsert_many(
        PLATFORM,
        PRODUCT_ID,
        [
            Review(
                platform=PLATFORM,
                product_id=PRODUCT_ID,
                review_id=rid,
                content=f"리뷰 {rid}",
                written_at=written_at,
            )
            for rid, written_at in _SEED
        ],
    )
    await session.commit()
    return session


async def test_single_page_returns_expected_order(seeded):
    rows, next_cursor = await ReviewRepository(seeded).list_page(
        PLATFORM, PRODUCT_ID, limit=10
    )
    assert [r.review_id for r in rows] == _EXPECTED_ORDER
    assert next_cursor is None, "마지막 페이지에는 다음 cursor 가 없어야 한다"


async def test_paging_covers_every_review_exactly_once(seeded):
    repo = ReviewRepository(seeded)
    seen: list[str] = []
    cursor = None
    for _ in range(10):  # 무한 루프 방지
        rows, cursor = await repo.list_page(PLATFORM, PRODUCT_ID, limit=2, cursor=cursor)
        seen.extend(r.review_id for r in rows)
        if cursor is None:
            break

    assert cursor is None, "페이지를 다 돌지 못했습니다"
    assert seen == _EXPECTED_ORDER, "누락되거나 중복된 리뷰가 있습니다"


async def test_paging_across_tie_and_null_boundaries(seeded):
    """동률 구간과 NULL 구간 경계에서 건너뛰거나 겹치지 않아야 한다."""
    repo = ReviewRepository(seeded)
    # r1, r3 까지 본 상태에서 이어받는다 — 다음은 동률의 나머지(r2)여야 한다.
    first, cursor = await repo.list_page(PLATFORM, PRODUCT_ID, limit=2)
    assert [r.review_id for r in first] == ["r1", "r3"]

    second, cursor = await repo.list_page(PLATFORM, PRODUCT_ID, limit=2, cursor=cursor)
    assert [r.review_id for r in second] == ["r2", "r4"]

    third, cursor = await repo.list_page(PLATFORM, PRODUCT_ID, limit=2, cursor=cursor)
    assert [r.review_id for r in third] == ["r6", "r5"]
    assert cursor is None


async def test_cursor_from_null_section_continues_within_nulls(seeded):
    repo = ReviewRepository(seeded)
    cursor = _encode_review_cursor(None, "r6")
    rows, _ = await repo.list_page(PLATFORM, PRODUCT_ID, limit=10, cursor=cursor)
    assert [r.review_id for r in rows] == ["r5"]


@pytest.mark.parametrize(
    "bad_cursor",
    [
        "이건유효하지않은커서",
        "!!!not-base64!!!",
        _encode_review_cursor(None, "ok")[:-3],  # 잘린 cursor
    ],
)
async def test_tampered_cursor_is_rejected(seeded, bad_cursor):
    with pytest.raises(InvalidCursorError):
        await ReviewRepository(seeded).list_page(PLATFORM, PRODUCT_ID, cursor=bad_cursor)


async def test_cursor_without_timezone_is_rejected(seeded):
    import base64
    import json

    naive = base64.urlsafe_b64encode(
        json.dumps(["2026-01-04T00:00:00", "r2"]).encode()
    ).decode()
    with pytest.raises(InvalidCursorError):
        await ReviewRepository(seeded).list_page(PLATFORM, PRODUCT_ID, cursor=naive)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_api_rejects_bad_cursor_with_400(engine, client):
    # 아직 수집된 적 없는 상품이어도 cursor 형식 오류는 400 이어야 한다.
    resp = client.get(f"/api/v1/{PLATFORM}/products/아직없는상품?cursor=깨진커서")

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_CURSOR"


def test_api_validation_error_uses_common_format(engine, client):
    resp = client.get(f"/api/v1/{PLATFORM}/products/{PRODUCT_ID}?limit=999")

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["detail"], "어느 필드가 틀렸는지 알려줘야 한다"


def test_demo_route_error_has_meaningful_code(engine, client):
    resp = client.get("/없는플랫폼/search?keyword=x")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


async def test_review_upsert_refreshes_updated_at(seeded):
    """재수집 시 updated_at 이 갱신되어야 한다.

    모델의 onupdate 는 ORM UPDATE 에만 걸리고 ON CONFLICT DO UPDATE 에는 적용되지
    않는다. 갱신되지 않으면 변경 추적과 운영 조사가 잘못된 시각을 보게 된다.
    """
    repo = ReviewRepository(seeded)
    rows, _ = await repo.list_page(PLATFORM, PRODUCT_ID, limit=1)
    before = rows[0].updated_at

    await repo.upsert_many(
        PLATFORM,
        PRODUCT_ID,
        [
            Review(
                platform=PLATFORM,
                product_id=PRODUCT_ID,
                review_id=rows[0].review_id,
                content="다시 수집한 내용",
                written_at=rows[0].written_at,
            )
        ],
    )
    await seeded.commit()

    # upsert 는 Core 문장이라 세션의 identity map 을 갱신하지 않는다. 실제 경로에서는
    # 워커가 저장하고 API 가 별개 세션으로 읽으므로, 여기서도 캐시를 비우고 다시 읽는다.
    seeded.expire_all()
    rows_after, _ = await repo.list_page(PLATFORM, PRODUCT_ID, limit=1)
    assert rows_after[0].content == "다시 수집한 내용"
    assert rows_after[0].updated_at > before, "재수집했는데 updated_at 이 그대로입니다."


async def test_product_upsert_refreshes_updated_at(session):
    product = Product(platform=PLATFORM, product_id="upd-1", name="처음", url="https://a")
    repo = ProductRepository(session)
    await repo.upsert(product)
    await session.commit()
    before = (await repo.get(PLATFORM, "upd-1")).updated_at

    await repo.upsert(
        Product(platform=PLATFORM, product_id="upd-1", name="바뀜", url="https://b")
    )
    await session.commit()

    session.expire_all()
    row = await repo.get(PLATFORM, "upd-1")
    assert row.name == "바뀜"
    assert row.updated_at > before, "재수집했는데 updated_at 이 그대로입니다."
