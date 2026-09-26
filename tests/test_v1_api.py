"""GET /api/v1 응답 형식 통합 테스트 (issue #10, specs/2026-09-03-api-contract.md 기준).

TestClient는 컨텍스트 매니저로 열어야 요청들이 하나의 이벤트 루프(portal)를
공유한다 — 그래야 v1._session_factory() 의 lru_cache 엔진이 요청마다 다른
루프에서 재사용되는 사고가 안 난다.
"""

import pytest
from fastapi.testclient import TestClient

from review_data.api.app import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_cold_start_returns_202_queued(engine, client):
    resp = client.get("/api/v1/apiplat/products/api-1")

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["job"]["status"] == "pending"
    assert "product" not in body


def test_repeated_request_reuses_same_job(engine, client):
    first = client.get("/api/v1/apiplat/products/api-2").json()
    second = client.get("/api/v1/apiplat/products/api-2").json()

    assert first["job"]["id"] == second["job"]["id"]


def test_missing_job_returns_common_error_format(engine, client):
    resp = client.get("/api/v1/jobs/999999999")

    assert resp.status_code == 404
    assert resp.json() == {
        "error": {"code": "NOT_FOUND", "message": "job을 찾을 수 없습니다.", "detail": None}
    }
