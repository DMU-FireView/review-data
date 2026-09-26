"""
JS 렌더링이 필요한 플랫폼용 베이스.

BaseCollector 를 상속하므로 self.client (httpx) 도 그대로 쓸 수 있습니다.
-> 상품은 API, 리뷰는 렌더링 같은 혼합 방식도 가능합니다.

브라우저는 collector 당 1개만 띄우고 재사용합니다.
페이지가 필요할 때마다 self.page() 컨텍스트를 사용하세요.

    async with self.page() as page:
        await page.goto(url)
        html = await page.content()
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from playwright.async_api import Browser, Page, Playwright, async_playwright

from review_data.core.base import DEFAULT_HEADERS, BaseCollector


class BrowserCollector(BaseCollector):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def setup(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.settings.headless
        )

    async def teardown(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    @asynccontextmanager
    async def page(self) -> AsyncIterator[Page]:
        """새 브라우저 컨텍스트에서 페이지를 열고, 블록을 벗어나면 자동으로 닫습니다."""
        if self._browser is None:
            raise RuntimeError(
                f"[{self.platform}] 'async with' 블록 안에서만 page 를 사용할 수 있습니다."
            )
        context = await self._browser.new_context(
            user_agent=DEFAULT_HEADERS["User-Agent"],
            locale="ko-KR",
        )
        page = await context.new_page()
        try:
            yield page
        finally:
            await context.close()