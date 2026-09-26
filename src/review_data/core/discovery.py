"""
collectors/ 하위 패키지를 스캔해 BaseCollector 구현체를 자동 등록합니다.

중앙 레지스트리 파일이 없으므로 팀원은 자기 폴더만 건드리면 됩니다.
(공용 파일 수정 -> 머지 충돌이 구조적으로 발생하지 않음)

로드 실패는 절대 조용히 삼키지 않고 원인(트레이스백)을 그대로 보여줍니다.
-> 오타/문법 오류가 '미구현'으로 잘못 표시되는 일이 없습니다.
"""

import importlib
import importlib.util
import inspect
import pkgutil
import traceback
from dataclasses import dataclass

from review_data import collectors
from review_data.core.base import BaseCollector
from review_data.core.browser import BrowserCollector

# 구현체가 아니라 뼈대인 클래스들 (등록 대상에서 제외)
_ABSTRACT_BASES = {BaseCollector, BrowserCollector}

COLLECTOR_MODULE_NAME = "collector"


@dataclass(frozen=True)
class LoadFailure:
    """collector 로드 중 발생한 오류."""

    package: str
    traceback_text: str


def discover() -> tuple[dict[str, type[BaseCollector]], list[LoadFailure]]:
    """(platform -> collector 클래스, 로드 실패 목록) 을 반환합니다."""
    registry: dict[str, type[BaseCollector]] = {}
    failures: list[LoadFailure] = []

    for module_info in pkgutil.iter_modules(collectors.__path__):
        # _template 등 언더스코어로 시작하는 패키지는 제외
        if not module_info.ispkg or module_info.name.startswith("_"):
            continue

        module_path = f"{collectors.__name__}.{module_info.name}.{COLLECTOR_MODULE_NAME}"

        # collector.py 자체가 없는 폴더는 '아직 작업 전' 이므로 조용히 건너뜀
        try:
            if importlib.util.find_spec(module_path) is None:
                continue
        except (ImportError, AttributeError, ValueError):
            failures.append(LoadFailure(module_info.name, traceback.format_exc()))
            continue

        try:
            module = importlib.import_module(module_path)
        except Exception:  # noqa: BLE001 - 어떤 오류든 원인을 그대로 보여준다
            failures.append(LoadFailure(module_info.name, traceback.format_exc()))
            continue

        registry.update(_extract_collectors(module))

    return registry, failures


def _extract_collectors(module: object) -> dict[str, type[BaseCollector]]:
    found: dict[str, type[BaseCollector]] = {}

    for obj in vars(module).values():
        if not inspect.isclass(obj):
            continue
        if not issubclass(obj, BaseCollector):
            continue
        if obj in _ABSTRACT_BASES:
            continue
        # import 해온 남의 클래스는 제외 (이 모듈에서 정의한 것만)
        if obj.__module__ != module.__name__:  # type: ignore[attr-defined]
            continue
        # 추상 메서드를 다 구현하지 않은 클래스는 제외
        if inspect.isabstract(obj):
            continue
        platform = getattr(obj, "platform", None)
        if not platform:
            continue
        found[platform] = obj

    return found