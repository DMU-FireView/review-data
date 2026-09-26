"""
ORM 모델과 Alembic 마이그레이션이 어긋나지 않는지 검증 (issue #27).

나머지 통합 테스트는 `Base.metadata.create_all()` 로 스키마를 만든다. 빠르지만
마이그레이션을 전혀 검증하지 못해서, 실제로 datetime 컬럼의 `timezone=True` 누락을
이 방식으로는 놓쳤다. 여기서는 마이그레이션으로만 스키마를 만들고 모델과 비교한다.
"""

import os
import subprocess
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from review_data.core.db import models  # noqa: F401 - Base.metadata 등록용
from review_data.core.db.base import Base

from .conftest import DATABASE_URL

# 이 파일은 마이그레이션만으로 스키마를 만들어야 하므로 별도 DB 를 쓴다.
# 다른 테스트가 create_all 로 만든 스키마와 섞이면 검증 자체가 무의미해진다.
_DRIFT_DB = "review_data_drift"

# 비교에서 제외할 대상. alembic_version 은 마이그레이션 도구가 관리하는 테이블이다.
_IGNORED_TABLES = {"alembic_version"}


def _drift_url() -> str:
    return make_url(DATABASE_URL).set(database=_DRIFT_DB).render_as_string(
        hide_password=False
    )


async def _recreate_drift_db() -> None:
    admin = create_async_engine(
        make_url(DATABASE_URL).set(database="postgres").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{_DRIFT_DB}"'))
            await conn.execute(text(f'CREATE DATABASE "{_DRIFT_DB}"'))
    finally:
        await admin.dispose()


@pytest.fixture(scope="module")
async def migrated_url():
    """빈 DB 에 alembic upgrade head 만 적용하고 그 접속 주소를 준다.

    alembic 을 별도 프로세스로 돌린다. env.py 가 자체 이벤트 루프를 열기 때문에
    실행 중인 루프 안에서는 부를 수 없고, 무엇보다 이렇게 하면 실제 배포에서
    쓰는 것과 똑같은 경로를 검증하게 된다.
    """
    try:
        await _recreate_drift_db()
    except Exception:
        pytest.skip("로컬 Postgres에 연결할 수 없어 스키마 검증을 건너뜁니다.")

    url = _drift_url()
    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"마이그레이션 실행 실패:\n{result.stderr}"

    yield url


async def test_migrations_match_orm_models(migrated_url):
    """마이그레이션으로 만든 스키마가 ORM 모델과 일치해야 한다.

    차이가 나면 모델만 고치고 마이그레이션을 빼먹었거나 그 반대다.
    """
    engine = create_async_engine(migrated_url)
    try:
        async with engine.connect() as conn:
            diffs = await conn.run_sync(
                lambda sync_conn: compare_metadata(
                    MigrationContext.configure(
                        sync_conn,
                        opts={"include_name": lambda name, type_, parent: not (
                            type_ == "table" and name in _IGNORED_TABLES
                        )},
                    ),
                    Base.metadata,
                )
            )
    finally:
        await engine.dispose()

    assert diffs == [], (
        "ORM 모델과 마이그레이션이 어긋납니다. 모델을 바꾸고 마이그레이션을 "
        f"빼먹었는지 확인하세요:\n{diffs}"
    )


def test_migration_history_has_single_head():
    """head 가 여러 개면 누군가 같은 부모에서 마이그레이션을 각자 만든 것이다."""
    heads = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
    assert len(heads) == 1, f"head 가 여러 개입니다(병합 필요): {heads}"
