"""cli.py / manual_entry.py 가 storage.py(JSON) 대신 DB에 실제로 쌓이는지 확인 (issue #12).

conftest.py 의 engine fixture가 로컬 Postgres 접속을 시도하며, 접속 실패 시
이 파일의 테스트는 모두 skip된다. cli.py/manual_entry.py 는 자체 session_scope()로
자기 엔진을 관리하므로(=DATABASE_URL을 그대로 씀), session fixture 대신 engine
fixture만 받아 스키마 준비만 보장한다.
"""

from review_data.cli import _collect_products, _collect_reviews
from review_data.collectors.auction import manual_entry
from review_data.core.base import BaseCollector
from review_data.core.db.base import session_scope
from review_data.core.db.repository import ProductRepository, ReviewRepository
from review_data.core.models import Product, Review


class _CliStubCollector(BaseCollector):
    platform = "clistubplat"

    async def search_products(self, keyword: str, limit: int = 20) -> list[Product]:
        return [
            Product(platform=self.platform, product_id="cli-1", name="CLI상품", url="https://x")
        ]

    async def get_product(self, product_id: str) -> Product:
        return Product(platform=self.platform, product_id=product_id, name="CLI상품", url="https://x")

    async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
        return [
            Review(platform=self.platform, product_id=product_id, review_id="cr1", content="굿")
        ]


async def test_cli_collect_products_saves_to_db(engine):
    registry = {"clistubplat": _CliStubCollector}
    await _collect_products(registry, ["clistubplat"], keyword="아무거나", limit=5, do_save=True)

    async with session_scope() as session:
        row = await ProductRepository(session).get("clistubplat", "cli-1")
    assert row is not None
    assert row.name == "CLI상품"


async def test_cli_reviews_saves_product_and_reviews_to_db(engine):
    await _collect_reviews(_CliStubCollector, "clistubplat", "cli-2", limit=5, do_save=True)

    async with session_scope() as session:
        product_row = await ProductRepository(session).get("clistubplat", "cli-2")
        review_rows, _ = await ReviewRepository(session).list_page("clistubplat", "cli-2")

    assert product_row is not None
    assert len(review_rows) == 1
    assert review_rows[0].review_id == "cr1"


async def test_manual_entry_saves_products(engine):
    products = [
        Product(platform="auction", product_id="manual-1", name="수동상품", url="https://x")
    ]
    await manual_entry._save_products(products)

    async with session_scope() as session:
        row = await ProductRepository(session).get("auction", "manual-1")
    assert row is not None
    assert row.name == "수동상품"


async def test_manual_entry_reviews_creates_placeholder_product(engine, monkeypatch):
    # 대상 product_id가 DB에 없으면 최소 정보를 물어봐서 먼저 만든다.
    answers = iter(["플레이스홀더 상품", "https://placeholder"])
    monkeypatch.setattr(manual_entry, "_ask", lambda *a, **k: next(answers))

    reviews = [
        Review(platform="auction", product_id="manual-2", review_id="mr1", content="좋아요")
    ]
    await manual_entry._save_reviews("manual-2", reviews)

    async with session_scope() as session:
        product_row = await ProductRepository(session).get("auction", "manual-2")
        review_rows, _ = await ReviewRepository(session).list_page("auction", "manual-2")

    assert product_row is not None
    assert product_row.name == "플레이스홀더 상품"
    assert len(review_rows) == 1
