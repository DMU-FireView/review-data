"""
DB가 필요한 통합 테스트용 공통 fixture.

개발용 DB 와 분리된 테스트 전용 DB(`<이름>_test`)를 쓴다. 테스트는 스키마를
만들고 지우므로 개발 DB 를 그대로 쓰면 작업 중인 데이터와 마이그레이션 상태가
날아간다. 접속할 수 없으면 해당 모듈 전체를 skip 한다 — 로컬에 Postgres 가 없어도
나머지 테스트는 그대로 돈다.

engine(모듈 스코프)은 스키마 생성/정리만 담당한다. asyncpg 연결은 이벤트 루프에
묶이는데, pytest-asyncio 기본값은 테스트마다 새 루프를 쓰므로 실제로 쓰는 엔진은
테스트마다 새로 만든다.
"""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from review_data.core.db.base import Base, create_session_factory
from review_data.core.settings import get_settings

# 테스트는 스키마를 만들고 지우므로 개발용 DB 를 그대로 쓰면 안 된다. 같은 서버의
# 별도 DB(_test 접미사)를 쓰고, 없으면 만든다. TEST_DATABASE_URL 로 직접 지정할 수도 있다.
_BASE_URL = os.environ.get("DATABASE_URL") or get_settings().database_url


def _test_database_url() -> str:
    override = os.environ.get("TEST_DATABASE_URL")
    if override:
        return override
    url = make_url(_BASE_URL)
    name = url.database or "review_data"
    if name.endswith("_test"):
        return _BASE_URL
    # str(URL) 은 비밀번호를 *** 로 가리므로 접속에 쓸 수 없다.
    return url.set(database=f"{name}_test").render_as_string(hide_password=False)


DATABASE_URL = _test_database_url()


async def _ensure_test_database() -> None:
    """테스트 DB 가 없으면 만든다. CREATE DATABASE 는 트랜잭션 밖에서만 된다."""
    url = make_url(DATABASE_URL)
    admin = create_async_engine(
        url.set(database="postgres").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with admin.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": url.database},
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    finally:
        await admin.dispose()


@pytest.fixture(scope="module")
async def engine():
    try:
        await _ensure_test_database()
    except Exception:
        pytest.skip("로컬 Postgres에 연결할 수 없어 통합 테스트를 건너뜁니다 (DATABASE_URL 확인).")

    eng = create_async_engine(DATABASE_URL)
    try:
        async with eng.connect():
            pass
    except Exception:
        await eng.dispose()
        pytest.skip("테스트 DB에 연결할 수 없어 통합 테스트를 건너뜁니다.")

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await eng.dispose()

    yield None

    eng = create_async_engine(DATABASE_URL)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest.fixture
async def _clean_db(engine):
    """각 테스트가 이전 테스트의 commit된 row에 영향받지 않도록 매번 비운다.

    session fixture의 rollback은 그 세션에서 커밋하지 않은 변경만 되돌리므로
    그것만으로는 부족하다. session과 session_factory가 함께 쓰여도 fixture 캐싱
    덕분에 이 정리는 테스트당 한 번만 돈다.
    """
    eng = create_async_engine(DATABASE_URL)
    async with eng.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())
    await eng.dispose()


@pytest.fixture
async def session(_clean_db):
    eng = create_async_engine(DATABASE_URL)
    factory = create_session_factory(eng)
    async with factory() as s:
        yield s
        await s.rollback()
    await eng.dispose()


@pytest.fixture
async def session_factory(_clean_db):
    """워커용. 워커는 짧은 트랜잭션을 여러 번 열므로 세션이 아니라 팩토리를 받는다."""
    eng = create_async_engine(DATABASE_URL)
    yield create_session_factory(eng)
    await eng.dispose()
