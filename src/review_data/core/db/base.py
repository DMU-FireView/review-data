"""
SQLAlchemy 엔진/세션 설정.

접속 정보는 core/settings.py 의 database_url 을 사용합니다.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from review_data.core.settings import get_settings


class Base(DeclarativeBase):
    """모든 ORM 모델의 공통 베이스."""


def create_engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_url)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """CLI/스크립트처럼 한 번 쓰고 버리는 짧은 프로세스용 세션.

    성공하면 commit, 예외가 나면 rollback 하고 엔진을 정리한다. FastAPI 앱처럼
    오래 떠 있는 프로세스는 lru_cache 엔진(예: api/v1.py)을 쓰는 게 맞고, 이건
    프로세스가 바로 끝나는 CLI 명령용이다.
    """
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            yield session
            await session.commit()
    finally:
        await engine.dispose()
