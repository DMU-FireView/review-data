"""
결과 확인용 FastAPI 앱 (얇은 껍데기).

수집 로직은 전부 collectors/ 에만 있습니다. 이 파일은 그것을 HTTP 로 노출만 합니다.
-> 본 서버 이식 시 core/ 와 collectors/ 만 가져가고 이 파일은 버리면 됩니다.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from review_data.api import v1
from review_data.api.sse import (
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_HEARTBEAT,
    EVENT_PROGRESS,
    EVENT_REVIEW,
    HEARTBEAT_INTERVAL,
    SSE_HEADERS,
    done_data,
    error_data,
    format_sse,
    parse_last_event_id,
    progress_data,
)
from review_data.core.base import BaseCollector
from review_data.core.db.base import create_engine, create_session_factory
from review_data.core.discovery import LoadFailure, discover
from review_data.core.exceptions import CollectorError, NotSupportedError
from review_data.core.models import Review


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """DB 엔진을 앱 수명에 묶는다.

    모듈 전역에 캐시해두면 엔진이 만들어질 때의 이벤트 루프에 asyncpg 커넥션이
    묶인 채 루프가 바뀌어도 재사용되고, 종료 시 커넥션 풀도 정리되지 않는다.
    """
    engine = create_engine()
    app.state.db_engine = engine
    app.state.session_factory = create_session_factory(engine)
    try:
        yield
    finally:
        await engine.dispose()


app = FastAPI(title="review-data", version="0.1.0", lifespan=lifespan)
app.include_router(v1.router)


# 상태 코드만 있고 code 가 지정되지 않은 오류(기존 데모 라우트)를 위한 기본 매핑.
# Spring 쪽이 error.code 로 분기할 수 있어야 하므로 전부 "ERROR" 로 뭉뚱그리지 않는다.
_STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    501: "NOT_SUPPORTED",
}


@app.exception_handler(HTTPException)
async def _handle_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
    """모든 엔드포인트가 {"error": {code, message, detail}} 형식으로 응답하게 통일한다.

    기존 데모 엔드포인트는 detail 에 그냥 문자열을 넣으므로, 그 경우는 상태 코드에
    맞는 code 를 붙여 같은 형식으로 감싸준다.
    """
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(
            status_code=exc.status_code, content=exc.detail, headers=exc.headers
        )

    code = _STATUS_CODES.get(exc.status_code, "INTERNAL_ERROR")
    body = {"error": {"code": code, "message": str(exc.detail), "detail": None}}
    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def _handle_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """검증 실패도 같은 오류 형식으로 내보낸다.

    FastAPI 기본 핸들러는 {"detail": [...]} 를 쓰는데, 그러면 호출 측이 오류 형식을
    두 가지로 나눠 파싱해야 한다.
    """
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "요청 값이 올바르지 않습니다.",
                "detail": jsonable_encoder(exc.errors()),
            }
        },
    )


@lru_cache
def _registry() -> tuple[dict[str, type[BaseCollector]], tuple[LoadFailure, ...]]:
    registry, failures = discover()
    return registry, tuple(failures)


def _get_collector_cls(platform: str) -> type[BaseCollector]:
    registry, _ = _registry()
    cls = registry.get(platform)
    if cls is None:
        raise HTTPException(
            status_code=404,
            detail=f"'{platform}' collector 가 없습니다. 사용 가능: {sorted(registry)}",
        )
    return cls


def _to_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotSupportedError):
        return HTTPException(status_code=501, detail=str(exc))
    if isinstance(exc, CollectorError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")


@app.get("/platforms")
async def platforms() -> dict:
    registry, failures = _registry()
    return {
        "available": sorted(registry.keys()),
        "failed": [f.package for f in failures],
    }


@app.get("/{platform}/search")
async def search(platform: str, keyword: str, limit: int = 20):
    collector_cls = _get_collector_cls(platform)
    try:
        async with collector_cls() as collector:
            return await collector.search_products(keyword, limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise _to_http_error(exc) from exc


@app.get("/{platform}/products/{product_id}")
async def product(platform: str, product_id: str):
    collector_cls = _get_collector_cls(platform)
    try:
        async with collector_cls() as collector:
            return await collector.get_product(product_id)
    except Exception as exc:  # noqa: BLE001
        raise _to_http_error(exc) from exc


@app.get("/{platform}/products/{product_id}/reviews")
async def reviews(platform: str, product_id: str, limit: int = 50):
    collector_cls = _get_collector_cls(platform)
    try:
        async with collector_cls() as collector:
            return await collector.get_reviews(product_id, limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise _to_http_error(exc) from exc


@app.get("/{platform}/products/{product_id}/reviews/stream")
async def reviews_stream(
    platform: str,
    product_id: str,
    limit: int = 50,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """리뷰를 수집하면서 SSE 로 흘려보냅니다.

    분석 서버(review-ai-new)가 수집이 끝날 때까지 붙잡혀 있지 않도록 만든 경로입니다.
    이벤트 계약은 api/sse.py 에 정리돼 있습니다.

    실패를 알리는 방법에는 경계가 있습니다. SSE 는 헤더를 먼저 보내기 때문입니다.
    - 스트림을 열기 **전에** 판정할 수 있는 실패(없는 platform, 빠진 인증 정보)는
      평소처럼 HTTP 상태 코드로 알립니다. 호출 측이 본문을 파싱하지 않고도 알 수 있습니다.
    - 스트림을 연 **뒤** 생긴 실패는 상태 코드가 이미 200 으로 나갔으므로 `error`
      이벤트로 알리고 닫습니다.
    """

    collector_cls = _get_collector_cls(platform)
    try:
        # 인증 정보 검사는 생성자에서 일어납니다. 스트림을 열기 전에 걸러 둡니다.
        collector = collector_cls()
    except Exception as exc:  # noqa: BLE001
        raise _to_http_error(exc) from exc

    return StreamingResponse(
        _iter_review_events(
            collector,
            product_id,
            limit=limit,
            job_id=uuid4().hex,
            skip=parse_last_event_id(last_event_id),
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


async def _iter_review_events(
    collector: BaseCollector,
    product_id: str,
    *,
    limit: int,
    job_id: str,
    skip: int,
) -> AsyncIterator[str]:
    """수집을 백그라운드로 돌리며 SSE 프레임을 내보냅니다.

    수집을 별도 task 로 돌리는 이유는 heartbeat 때문입니다. 수집이 조용한 동안에도
    이 generator 는 깨어나 heartbeat 를 보내야 연결이 살아 있음을 알릴 수 있습니다.
    """

    queue: asyncio.Queue[Review | Exception | None] = asyncio.Queue()

    async def produce() -> None:
        """수집 결과를 queue 로 넘깁니다. 끝나면 None 을 넣어 종료를 알립니다."""

        try:
            async with collector:
                async for review in collector.iter_reviews(product_id, limit=limit):
                    await queue.put(review)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await queue.put(exc)
            return
        await queue.put(None)

    task = asyncio.create_task(produce())
    # 재연결이면 이미 보낸 개수부터 이어서 셉니다. id 가 계속 늘어나야
    # 다음 Last-Event-ID 도 의미를 갖습니다.
    collected = skip
    remaining_skip = skip

    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL)
            except TimeoutError:
                # 수집이 조용한 것뿐입니다. 연결이 끊긴 것과 구분해 줍니다.
                yield format_sse(EVENT_HEARTBEAT, {})
                continue

            if item is None:
                yield format_sse(EVENT_DONE, done_data(job_id, collected))
                return

            if isinstance(item, Exception):
                # 수집 실패를 빈 스트림으로 감추지 않습니다.
                yield format_sse(EVENT_ERROR, error_data(job_id, item))
                return

            if remaining_skip > 0:
                # 재연결 전에 이미 보낸 리뷰입니다. 다시 보내지 않습니다.
                remaining_skip -= 1
                continue

            collected += 1
            yield format_sse(
                EVENT_REVIEW,
                item.model_dump(mode="json"),
                event_id=str(collected),
            )
            yield format_sse(EVENT_PROGRESS, progress_data(job_id, collected, limit))
    finally:
        # 호출 측이 연결을 끊으면 이 generator 가 닫힙니다. 수집 task 를 남기지 않습니다.
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
