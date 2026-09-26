"""리뷰 SSE 스트림 단위 테스트 (네트워크 없이 프레이밍/이벤트 순서만 검증)."""

import json
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from review_data.api import app as app_module
from review_data.api.sse import (
    classify_error,
    format_sse,
    parse_last_event_id,
)
from review_data.core.base import BaseCollector
from review_data.core.exceptions import (
    MissingCredentialError,
    NotSupportedError,
    ParseError,
)
from review_data.core.models import Review


def make_review(review_id: str) -> Review:
    return Review(
        platform="fake",
        product_id="p1",
        review_id=review_id,
        content=f"리뷰 {review_id}",
        rating=4.0,
        author="테스터",
        written_at=datetime(2026, 7, 15, 12, 0, 0),
        collected_at=datetime(2026, 8, 30, 9, 0, 0),
    )


class FakeCollector(BaseCollector):
    """리뷰 3건을 돌려주는 collector. iter_reviews 는 기본 구현을 그대로 씁니다."""

    platform = "fake"
    reviews = [make_review("r1"), make_review("r2"), make_review("r3")]
    failure: Exception | None = None

    async def search_products(self, keyword: str, limit: int = 20):
        raise NotSupportedError("test")

    async def get_product(self, product_id: str):
        raise NotSupportedError("test")

    async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
        if type(self).failure is not None:
            raise type(self).failure
        return type(self).reviews[:limit]


@pytest.fixture
def client(monkeypatch):
    """discovery 를 거치지 않고 FakeCollector 만 등록된 앱을 씁니다."""

    monkeypatch.setattr(
        app_module, "_registry", lambda: ({"fake": FakeCollector}, ())
    )
    FakeCollector.failure = None
    FakeCollector.reviews = [make_review("r1"), make_review("r2"), make_review("r3")]
    with TestClient(app_module.app) as test_client:
        yield test_client


def parse_sse(body: str) -> list[tuple[str, dict, str | None]]:
    """SSE 본문을 (event, data, id) 목록으로 되돌립니다."""

    events = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        name = None
        event_id = None
        data_lines = []
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("id: "):
                event_id = line.removeprefix("id: ")
            elif line.startswith("data: "):
                data_lines.append(line.removeprefix("data: "))
        events.append((name, json.loads("\n".join(data_lines)), event_id))
    return events


# ── 프레이밍 ────────────────────────────────────────


def test_format_sse_기본_프레임():
    frame = format_sse("review", {"a": 1})
    assert frame == 'event: review\ndata: {"a": 1}\n\n'


def test_format_sse_id_포함():
    frame = format_sse("review", {"a": 1}, event_id="7")
    assert frame.startswith("event: review\nid: 7\ndata: ")


def test_format_sse_줄바꿈은_data_줄로_나뉜다():
    frame = format_sse("error", {"detail": "첫 줄\n둘째 줄"})
    # JSON 직렬화가 \n 을 이스케이프하므로 data 줄은 하나로 유지됩니다.
    assert frame.count("data: ") == 1
    assert "\\n" in frame


def test_format_sse_한글은_그대로_나간다():
    frame = format_sse("error", {"detail": "수집 실패"})
    assert "수집 실패" in frame


def test_format_sse_datetime_직렬화():
    frame = format_sse("review", {"written_at": datetime(2026, 7, 15)})
    assert "2026-07-15" in frame


# ── 오류 분류 ────────────────────────────────────────


def test_classify_error_타임아웃은_재시도_가능():
    _, retryable = classify_error(httpx.ReadTimeout("timeout"))
    assert retryable is True


def test_classify_error_통신오류는_재시도_가능():
    _, retryable = classify_error(httpx.ConnectError("refused"))
    assert retryable is True


@pytest.mark.parametrize(
    "exc",
    [
        NotSupportedError("지원 안 함"),
        MissingCredentialError("키 없음"),
        ParseError("셀렉터 깨짐"),
        RuntimeError("알 수 없는 버그"),
    ],
)
def test_classify_error_나머지는_재시도_불가(exc):
    _, retryable = classify_error(exc)
    assert retryable is False


# ── Last-Event-ID ───────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 0), ("3", 3), (" 5 ", 5), ("0", 0), ("-2", 0), ("abc", 0), ("", 0)],
)
def test_parse_last_event_id(raw, expected):
    assert parse_last_event_id(raw) == expected


# ── 스트림 동작 ──────────────────────────────────────


def test_스트림이_sse_content_type을_돌려준다(client):
    with client.stream("GET", "/fake/products/p1/reviews/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")


def test_리뷰마다_review와_progress가_나가고_done으로_끝난다(client):
    body = client.get("/fake/products/p1/reviews/stream").text
    events = parse_sse(body)

    names = [name for name, _, _ in events]
    assert names == [
        "review", "progress",
        "review", "progress",
        "review", "progress",
        "done",
    ]

    # review data 는 Review JSON 그대로다.
    first_review = events[0][1]
    assert first_review["review_id"] == "r1"
    assert first_review["platform"] == "fake"
    assert first_review["content"] == "리뷰 r1"

    # progress 는 누적 개수와 목표치를 함께 알린다.
    assert events[1][1]["collected"] == 1
    assert events[1][1]["target"] == 50
    assert events[5][1]["collected"] == 3

    # done 이 정상 종료 신호이며 최종 개수를 담는다.
    assert events[-1][1]["collected"] == 3


def test_job_id는_한_스트림_안에서_동일하다(client):
    events = parse_sse(client.get("/fake/products/p1/reviews/stream").text)
    job_ids = {data["job_id"] for name, data, _ in events if name in {"progress", "done"}}
    assert len(job_ids) == 1


def test_review_이벤트는_1부터의_일련번호_id를_갖는다(client):
    events = parse_sse(client.get("/fake/products/p1/reviews/stream").text)
    ids = [event_id for name, _, event_id in events if name == "review"]
    assert ids == ["1", "2", "3"]


def test_limit이_target으로_나간다(client):
    events = parse_sse(client.get("/fake/products/p1/reviews/stream?limit=2").text)
    progress = [data for name, data, _ in events if name == "progress"]
    assert all(item["target"] == 2 for item in progress)
    assert progress[-1]["collected"] == 2


def test_수집_실패는_error_이벤트로_나간다(client):
    FakeCollector.failure = ParseError("셀렉터가 깨졌습니다")
    events = parse_sse(client.get("/fake/products/p1/reviews/stream").text)

    assert [name for name, _, _ in events] == ["error"]
    data = events[0][1]
    assert "셀렉터가 깨졌습니다" in data["detail"]
    assert data["retryable"] is False
    # 실패했으므로 done 을 보내지 않는다.
    assert "done" not in [name for name, _, _ in events]


def test_통신_실패는_retryable_true로_나간다(client):
    FakeCollector.failure = httpx.ConnectError("연결 거부")
    events = parse_sse(client.get("/fake/products/p1/reviews/stream").text)
    assert events[0][0] == "error"
    assert events[0][1]["retryable"] is True


def test_리뷰가_없어도_done으로_끝난다(client):
    """리뷰 0건은 실패가 아니다. 판단은 분석 서버가 한다."""

    FakeCollector.reviews = []
    events = parse_sse(client.get("/fake/products/p1/reviews/stream").text)
    assert [name for name, _, _ in events] == ["done"]
    assert events[0][1]["collected"] == 0


def test_없는_platform은_스트림을_열기_전에_404(client):
    response = client.get("/nope/products/p1/reviews/stream")
    assert response.status_code == 404
    # SSE 가 아니라 평범한 JSON 오류로 나가야 한다.
    assert not response.headers["content-type"].startswith("text/event-stream")


def test_last_event_id를_주면_이미_받은_리뷰는_건너뛴다(client):
    body = client.get(
        "/fake/products/p1/reviews/stream",
        headers={"Last-Event-ID": "2"},
    ).text
    events = parse_sse(body)

    reviews = [(data["review_id"], event_id) for name, data, event_id in events if name == "review"]
    # 앞의 2건은 다시 보내지 않고, id 는 이어서 증가한다.
    assert reviews == [("r3", "3")]
    assert events[-1][1]["collected"] == 3
