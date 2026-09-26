"""내부 서버 토큰 인증 계약 테스트."""

import pytest
from fastapi.testclient import TestClient

from review_data.api.app import app
from review_data.core.settings import get_settings


@pytest.fixture(autouse=True)
def reset_settings_cache(monkeypatch):
    """환경변수를 바꾼 테스트끼리 설정 싱글턴을 공유하지 않게 한다."""
    monkeypatch.delenv("INTERNAL_TOKEN", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("configured_token", [None, ""])
def test_authentication_is_disabled_when_token_is_not_configured(
    monkeypatch, configured_token
):
    if configured_token is not None:
        monkeypatch.setenv("INTERNAL_TOKEN", configured_token)
        get_settings.cache_clear()

    response = TestClient(app).get("/platforms")

    assert response.status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        None,
        {"X-Internal-Token": "wrong-token"},
        # ASCII 가 아닌 값도 500 이 아니라 401 이어야 한다.
        {"X-Internal-Token": "잘못된토큰".encode()},
    ],
)
def test_missing_or_mismatched_token_returns_common_error(monkeypatch, headers):
    monkeypatch.setenv("INTERNAL_TOKEN", "correct-token")
    get_settings.cache_clear()

    response = TestClient(app).get("/platforms", headers=headers)

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "UNAUTHORIZED",
            "message": "유효한 X-Internal-Token이 필요합니다.",
            "detail": None,
        }
    }


@pytest.mark.parametrize(
    "path", ["/api/v1/jobs/1", "/unknown/products/1/reviews/stream"]
)
def test_v1_and_sse_routes_require_token(monkeypatch, path):
    monkeypatch.setenv("INTERNAL_TOKEN", "correct-token")
    get_settings.cache_clear()

    response = TestClient(app).get(path)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_matching_token_allows_request(monkeypatch):
    monkeypatch.setenv("INTERNAL_TOKEN", "correct-token")
    get_settings.cache_clear()

    response = TestClient(app).get(
        "/platforms", headers={"X-Internal-Token": "correct-token"}
    )

    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/health", "/docs", "/redoc", "/openapi.json"])
def test_public_paths_do_not_require_token(monkeypatch, path):
    monkeypatch.setenv("INTERNAL_TOKEN", "correct-token")
    get_settings.cache_clear()

    response = TestClient(app).get(path)

    assert response.status_code == 200


def test_openapi_exposes_internal_token_security_scheme():
    schema = TestClient(app).get("/openapi.json").json()

    assert schema["components"]["securitySchemes"]["APIKeyHeader"] == {
        "type": "apiKey",
        "description": "내부 서버 간 호출에 사용하는 공유 토큰",
        "in": "header",
        "name": "X-Internal-Token",
    }
    assert schema["paths"]["/platforms"]["get"]["security"] == [{"APIKeyHeader": []}]
