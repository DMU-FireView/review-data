"""에이블리 상품 및 공개 요약 리뷰 collector.

에이블리 native 검색 결과와 전체 리뷰 화면은 현재 Cloudflare 보안 확인
페이지로 전환됩니다. 보안 확인을 우회하지 않으며, 상품 검색은 NAVER
Developers 웹문서 검색의 외부 색인을 선택적 fallback으로 사용합니다.
외부 색인의 결과와 순위는 에이블리 자체 검색 결과가 아닙니다.
"""

import asyncio
import json
import logging
import re
import unicodedata
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Page, Response
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from review_data.core.browser import BrowserCollector
from review_data.core.exceptions import (
    MissingCredentialError,
    NotSupportedError,
    ParseError,
)
from review_data.core.models import Product, Review

SEARCH_URL = "https://m.a-bly.com/search"
PRODUCT_URL = "https://m.a-bly.com/goods/{product_id}"
BASIC_GOODS_PATH = "/api/v3/goods/{product_id}/basic/"
REVIEW_SUMMARY_PATH = "/api/v2/goods/{product_id}/review_summary/"
JSON_RESPONSE_TIMEOUT = 10.0
GOODS_API_GRACE_TIMEOUT = 3.0

NAVER_WEB_SEARCH_URL = "https://openapi.naver.com/v1/search/webkr.json"
NAVER_SEARCH_PAGE_SIZE = 100
NAVER_SEARCH_MAX_PAGES = 3
NAVER_DETAIL_CANDIDATE_MULTIPLIER = 5
ABLY_GOODS_PATH_PATTERN = re.compile(r"^/goods/(\d+)/?$")

SECURITY_TITLES = ("보안 확인 중", "잠시만 기다리십시오")

logger = logging.getLogger(__name__)


class AblyCollector(BrowserCollector):
    platform = "ably"
    label = "에이블리"

    async def search_products(self, keyword: str, limit: int = 20) -> list[Product]:
        if limit <= 0:
            return []

        keyword = keyword.strip()
        if not keyword:
            return []

        # ABLY native 검색은 결과 페이지가 Cloudflare 403, SEARCH_RESULT API가
        # 인증 없는 요청에 401을 반환하므로 사용하지 않습니다. 아래 결과는
        # NAVER의 외부 검색 색인 기반이며 ABLY 자체 검색 결과/순위가 아닙니다.
        return await self._search_products_external_index(keyword, limit)

    async def _search_products_external_index(
        self, keyword: str, limit: int
    ) -> list[Product]:
        client_id = self.settings.naver_client_id
        client_secret = self.settings.naver_client_secret
        if not client_id or not client_secret:
            raise MissingCredentialError(
                f"[{self.platform}] native 검색은 지원되지 않으며 NAVER external "
                "search index fallback을 사용하려면 NAVER_CLIENT_ID와 "
                "NAVER_CLIENT_SECRET이 필요합니다."
            )

        logger.info(
            "[%s] NAVER external search index fallback 사용: "
            "ABLY native 검색 결과/순위가 아닙니다. keyword=%r limit=%d",
            self.platform,
            keyword,
            limit,
        )

        headers = {
            "X-Naver-Client-Id": client_id,
            "X-Naver-Client-Secret": client_secret,
        }
        query = f"site:m.a-bly.com/goods {keyword}"
        max_candidates = min(
            NAVER_SEARCH_PAGE_SIZE * NAVER_SEARCH_MAX_PAGES,
            max(limit * NAVER_DETAIL_CANDIDATE_MULTIPLIER, limit),
        )

        candidate_ids: list[str] = []
        seen_ids: set[str] = set()
        naver_item_count = 0
        ably_url_count = 0

        for page_index in range(NAVER_SEARCH_MAX_PAGES):
            if page_index:
                await self.polite_wait()

            start = page_index * NAVER_SEARCH_PAGE_SIZE + 1
            response = await self.client.get(
                NAVER_WEB_SEARCH_URL,
                headers=headers,
                params={
                    "query": query,
                    "display": NAVER_SEARCH_PAGE_SIZE,
                    "start": start,
                    "sort": "sim",
                },
            )
            data = self._parse_naver_search_response(response.status_code, response)
            raw_items = data.get("items")
            if not isinstance(raw_items, list):
                raise ParseError(
                    f"[{self.platform}] NAVER external search index 응답의 "
                    "items 형식이 올바르지 않습니다."
                )

            naver_item_count += len(raw_items)
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                product_id = self._extract_ably_product_id(item.get("link"))
                if product_id is None:
                    continue
                ably_url_count += 1
                if product_id in seen_ids:
                    continue
                seen_ids.add(product_id)
                candidate_ids.append(product_id)
                if len(candidate_ids) >= max_candidates:
                    break

            if len(candidate_ids) >= max_candidates:
                break
            if len(raw_items) < NAVER_SEARCH_PAGE_SIZE:
                break
            total = self._optional_int(data.get("total"))
            if total is not None and start + len(raw_items) > total:
                break

        products: list[Product] = []
        detail_success_count = 0
        for candidate_index, product_id in enumerate(candidate_ids):
            if candidate_index:
                await self.polite_wait()
            try:
                product = await self.get_product(product_id)
            except Exception as exc:  # noqa: BLE001 - 개별 색인 후보 실패는 건너뜁니다.
                logger.warning(
                    "[%s] external fallback 상품 상세 확인 실패: "
                    "product_id=%s error_type=%s; 후보를 건너뜁니다.",
                    self.platform,
                    product_id,
                    type(exc).__name__,
                )
                continue

            detail_success_count += 1
            if not self._is_keyword_relevant(product, keyword):
                logger.info(
                    "[%s] external fallback 관련성 불충분 후보 제외: product_id=%s",
                    self.platform,
                    product_id,
                )
                continue

            products.append(product)
            if len(products) >= limit:
                break

        logger.info(
            "[%s] external fallback 완료: naver_items=%d ably_urls=%d "
            "unique_candidates=%d get_product_success=%d returned=%d",
            self.platform,
            naver_item_count,
            ably_url_count,
            len(candidate_ids),
            detail_success_count,
            len(products),
        )
        return products

    def _parse_naver_search_response(
        self, status_code: int, response: Any
    ) -> dict[str, Any]:
        if status_code in {401, 403}:
            raise ParseError(
                f"[{self.platform}] NAVER external search index 인증 실패: "
                f"HTTP {status_code}"
            )
        if status_code != 200:
            error_code = None
            try:
                error_data = response.json()
                if isinstance(error_data, dict):
                    error_code = self._optional_str(error_data.get("errorCode"))
            except (json.JSONDecodeError, TypeError):
                pass
            suffix = f" error_code={error_code}" if error_code else ""
            raise ParseError(
                f"[{self.platform}] NAVER external search index 요청 실패: "
                f"HTTP {status_code}{suffix}"
            )

        try:
            data = response.json()
        except (json.JSONDecodeError, TypeError) as exc:
            raise ParseError(
                f"[{self.platform}] NAVER external search index JSON을 파싱하지 못했습니다."
            ) from exc
        if not isinstance(data, dict):
            raise ParseError(
                f"[{self.platform}] NAVER external search index 응답 형식이 올바르지 않습니다."
            )
        return data

    @staticmethod
    def _extract_ably_product_id(link: object) -> str | None:
        if not isinstance(link, str):
            return None
        parsed = urlparse(link.strip())
        if parsed.scheme != "https" or parsed.hostname != "m.a-bly.com":
            return None
        try:
            port = parsed.port
        except ValueError:
            return None
        if parsed.username is not None or parsed.password is not None or port:
            return None
        match = ABLY_GOODS_PATH_PATTERN.fullmatch(parsed.path)
        return match.group(1) if match else None

    @classmethod
    def _is_keyword_relevant(cls, product: Product, keyword: str) -> bool:
        normalized_keyword = cls._normalize_search_text(keyword)
        if not normalized_keyword:
            return True

        searchable = cls._normalize_search_text(
            " ".join(value for value in (product.name, product.category) if value)
        )
        if normalized_keyword in searchable:
            return True

        tokens = [
            cls._normalize_search_text(token)
            for token in re.findall(r"[0-9A-Za-z가-힣]+", keyword)
        ]
        meaningful_tokens = [token for token in tokens if len(token) >= 2]
        return len(meaningful_tokens) > 1 and all(
            token in searchable for token in meaningful_tokens
        )

    @staticmethod
    def _normalize_search_text(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).casefold()
        return "".join(character for character in normalized if character.isalnum())

    async def get_product(self, product_id: str) -> Product:
        url = PRODUCT_URL.format(product_id=product_id)
        async with self.page() as page:
            goods_future = self._capture_json_response(
                page, BASIC_GOODS_PATH.format(product_id=product_id)
            )
            review_future = self._capture_review_summary(page, product_id)
            await self._open_page(page, url)
            json_ld = await self._read_product_json_ld(page)
            goods_response = await self._wait_for_json_response(
                goods_future,
                timeout=(
                    GOODS_API_GRACE_TIMEOUT
                    if isinstance(json_ld, dict)
                    else JSON_RESPONSE_TIMEOUT
                ),
            )
            goods = (
                goods_response.get("goods")
                if isinstance(goods_response, dict)
                else None
            )
            if not isinstance(goods, dict):
                if not isinstance(json_ld, dict):
                    goods = await self._read_goods_data(page, product_id)
                else:
                    goods = {}
            review_data = await self._wait_for_json_response(review_future)

        actual_id = self._optional_str(goods.get("sno"))
        name = self._optional_str(goods.get("name"))
        if isinstance(json_ld, dict):
            actual_id = actual_id or self._optional_str(json_ld.get("productID"))
            name = name or self._optional_str(json_ld.get("name"))
        if not actual_id or not name:
            raise ParseError(f"[{self.platform}] 상품 필수 필드(sno/name)를 찾지 못했습니다.")
        if actual_id != str(product_id):
            raise ParseError(
                f"[{self.platform}] 요청 상품 ID({product_id})와 응답 ID({actual_id})가 다릅니다."
            )

        price_info = goods.get("price_info")
        price = (
            self._optional_int(price_info.get("thumbnail_price"))
            if isinstance(price_info, dict)
            else None
        )
        if price is None and isinstance(json_ld, dict):
            offers = json_ld.get("offers")
            if isinstance(offers, dict):
                price = self._optional_int(offers.get("price"))

        cover_images = goods.get("cover_images")
        thumbnail_url = None
        if isinstance(cover_images, list) and cover_images:
            thumbnail_url = self._optional_str(cover_images[0])
        if thumbnail_url is None and isinstance(json_ld, dict):
            images = json_ld.get("image")
            if isinstance(images, list) and images:
                thumbnail_url = self._optional_str(images[0])
            elif isinstance(images, str):
                thumbnail_url = self._optional_str(images)

        market = goods.get("market")
        seller = (
            self._optional_str(market.get("name")) if isinstance(market, dict) else None
        )

        brand = None
        manufacturer = None
        category = None
        if isinstance(json_ld, dict):
            brand_data = json_ld.get("brand")
            if isinstance(brand_data, dict):
                brand = self._optional_str(brand_data.get("name"))
            manufacturer_data = json_ld.get("manufacturer")
            if isinstance(manufacturer_data, dict):
                manufacturer = self._optional_str(manufacturer_data.get("name"))
            category = self._optional_str(json_ld.get("category"))

        review_count = None
        if isinstance(review_data, dict):
            review_meta = review_data.get("review")
            if isinstance(review_meta, dict):
                review_count = self._optional_int(review_meta.get("count"))

        return Product(
            platform=self.platform,
            product_id=actual_id,
            name=name,
            url=url,
            brand=brand,
            manufacturer=manufacturer,
            seller=seller,
            price=price,
            thumbnail_url=thumbnail_url,
            category=category,
            review_count=review_count,
            rating=None,
        )

    async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
        if limit <= 0:
            return []

        url = PRODUCT_URL.format(product_id=product_id)
        async with self.page() as page:
            review_future = self._capture_review_summary(page, product_id)
            await self._open_page(page, url)
            review_data = await self._wait_for_json_response(review_future)

        if not isinstance(review_data, dict):
            raise NotSupportedError(
                f"[{self.platform}] 공개 요약 리뷰 응답을 확인할 수 없습니다."
            )

        raw_reviews = review_data.get("review_summary")
        if not isinstance(raw_reviews, list):
            raise ParseError(f"[{self.platform}] 요약 리뷰 목록 형식이 올바르지 않습니다.")

        reviews: list[Review] = []
        seen_ids: set[str] = set()
        for item in raw_reviews:
            review = self._parse_review(item, str(product_id))
            if review is None or review.review_id in seen_ids:
                continue
            seen_ids.add(review.review_id)
            reviews.append(review)
            if len(reviews) >= limit:
                break
        return reviews

    async def _open_page(self, page: Page, url: str) -> None:
        try:
            response = await page.goto(url, wait_until="domcontentloaded")
        except PlaywrightTimeoutError as exc:
            raise ParseError(f"[{self.platform}] 페이지 로딩 시간이 초과됐습니다: {url}") from exc

        status = response.status if response is not None else None
        if status == 403 or await self._is_security_page(page):
            raise ParseError(f"[{self.platform}] 에이블리 보안 확인 페이지가 표시됐습니다.")
        if status is not None and status >= 400:
            raise ParseError(f"[{self.platform}] 페이지 요청 실패: HTTP {status} ({url})")

    async def _is_security_page(self, page: Page) -> bool:
        title = await page.title()
        return any(marker in title for marker in SECURITY_TITLES)

    def _capture_review_summary(
        self, page: Page, product_id: str
    ) -> asyncio.Future[Response]:
        expected_path = REVIEW_SUMMARY_PATH.format(product_id=product_id)
        return self._capture_json_response(page, expected_path)

    @staticmethod
    def _capture_json_response(
        page: Page, expected_path: str
    ) -> asyncio.Future[Response]:
        future: asyncio.Future[Response] = asyncio.get_running_loop().create_future()

        def handle_response(response: Response) -> None:
            if future.done():
                return
            if urlparse(response.url).path == expected_path:
                future.set_result(response)

        page.on("response", handle_response)
        return future

    async def _wait_for_json_response(
        self,
        future: asyncio.Future[Response],
        timeout: float = JSON_RESPONSE_TIMEOUT,
    ) -> dict[str, Any] | None:
        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            return None

        if response.status != 200:
            return None
        try:
            data = await response.json()
        except (json.JSONDecodeError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    async def _read_goods_data(self, page: Page, product_id: str) -> dict[str, Any]:
        locator = page.locator("script#__NEXT_DATA__")
        try:
            await locator.wait_for(state="attached")
            raw_data = await locator.text_content()
        except PlaywrightTimeoutError as exc:
            raise ParseError(f"[{self.platform}] __NEXT_DATA__를 찾지 못했습니다.") from exc

        if not raw_data:
            raise ParseError(f"[{self.platform}] __NEXT_DATA__가 비어 있습니다.")

        try:
            next_data = json.loads(raw_data)
            queries = next_data["props"]["pageProps"]["serverQueryClient"]["queries"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ParseError(f"[{self.platform}] __NEXT_DATA__ 구조가 예상과 다릅니다.") from exc

        for query in queries:
            if not isinstance(query, dict):
                continue
            state = query.get("state")
            data = state.get("data") if isinstance(state, dict) else None
            goods = data.get("goods") if isinstance(data, dict) else None
            if (
                isinstance(goods, dict)
                and self._optional_str(goods.get("sno")) == str(product_id)
            ):
                return goods

        raise ParseError(f"[{self.platform}] __NEXT_DATA__에서 상품 데이터를 찾지 못했습니다.")

    async def _read_product_json_ld(self, page: Page) -> dict[str, Any] | None:
        for raw_data in await page.locator(
            'script[type="application/ld+json"]'
        ).all_text_contents():
            try:
                data = json.loads(raw_data)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("@type") == "Product":
                return data
        return None

    def _parse_review(self, item: object, product_id: str) -> Review | None:
        if not isinstance(item, dict):
            return None

        review_id = self._optional_str(item.get("sno"))
        content = self._optional_str(item.get("contents"))
        if not review_id or not content:
            return None

        raw_images = item.get("images")
        images = (
            [
                image
                for value in raw_images
                if (image := self._optional_str(value)) is not None
            ]
            if isinstance(raw_images, list)
            else []
        )

        return Review(
            platform=self.platform,
            product_id=product_id,
            review_id=review_id,
            content=content,
            rating=None,
            author=None,
            written_at=None,
            option=None,
            images=images,
            helpful_count=None,
        )

    @staticmethod
    def _optional_str(value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _optional_int(value: object) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
