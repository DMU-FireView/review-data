"""
리뷰 수집 SSE 스트림의 이벤트 계약과 프레이밍.

이 모듈은 네트워크도 collector 도 모릅니다. 순수 함수만 두어 단위 테스트로 검증합니다.
app.py 는 이 함수들이 만든 문자열을 그대로 흘려보내기만 합니다.

⚠️ 이벤트 이름과 data 형태는 분석 서버(review-ai-new)와의 계약입니다.
   수신 구현은 그쪽 `app/integrations/crawler_stream.py` 와 `app/schemas/stream.py` 에 있습니다.
   한쪽만 바꾸면 스트림이 조용히 깨지므로 변경은 양쪽 PR 로 함께 진행하세요.

이벤트는 다음 5개뿐입니다.

    | event       | data                                                      |
    |-------------|-----------------------------------------------------------|
    | review      | Review JSON object 1건                                     |
    | progress    | {"job_id": str, "collected": int, "target": int | null}    |
    | done        | {"job_id": str, "collected": int}                          |
    | error       | {"job_id": str, "detail": str, "retryable": bool}          |
    | heartbeat   | {}                                                         |

- `done` 이 정상 종료 신호입니다. `done` 없이 끊긴 스트림은 성공이 아닙니다.
- 수집 실패는 반드시 `error` 로 알립니다. 빈 스트림을 조용히 닫아 "리뷰 0건"처럼
  보이게 하지 않습니다.
- `target` 을 모르면 null 로 보냅니다. 0 으로 채우지 않습니다.
"""

import json
from typing import Any

import httpx

from review_data.core.exceptions import (
    CollectorError,
    MissingCredentialError,
    NotSupportedError,
)

EVENT_REVIEW = "review"
EVENT_PROGRESS = "progress"
EVENT_DONE = "done"
EVENT_ERROR = "error"
EVENT_HEARTBEAT = "heartbeat"

SSE_HEADERS = {
    "Cache-Control": "no-store",
    # nginx 가 스트림을 버퍼링해 진행 상황을 뭉쳐 보내지 않도록 합니다.
    "X-Accel-Buffering": "no",
}
"""SSE 응답에 붙이는 헤더."""

HEARTBEAT_INTERVAL = 15.0
"""이 시간(초) 동안 내보낼 리뷰가 없으면 heartbeat 를 보냅니다.

수집은 플랫폼 응답에 좌우돼 수십 초씩 조용할 수 있습니다. 그 침묵을 연결 끊김과
구분해 주려는 것입니다. 수신 측 read timeout 은 청크 사이 간격에 걸리므로,
heartbeat 가 그 타이머를 계속 갱신해 줍니다.
"""


def format_sse(event: str, data: Any, *, event_id: str | None = None) -> str:
    """SSE 프레임 하나를 만듭니다.

    data 는 JSON 으로 직렬화합니다. 직렬화 결과에 줄바꿈이 있어도 깨지지 않도록
    여러 줄이면 `data:` 를 줄마다 붙입니다(SSE 규격).
    """

    payload = json.dumps(data, ensure_ascii=False, default=str)

    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.extend(f"data: {line}" for line in payload.split("\n"))
    # 빈 줄 하나가 이벤트의 끝을 뜻합니다.
    return "\n".join(lines) + "\n\n"


def progress_data(job_id: str, collected: int, target: int | None) -> dict:
    """`progress` 이벤트 data."""

    return {"job_id": job_id, "collected": collected, "target": target}


def done_data(job_id: str, collected: int) -> dict:
    """`done` 이벤트 data."""

    return {"job_id": job_id, "collected": collected}


def error_data(job_id: str, exc: Exception) -> dict:
    """`error` 이벤트 data. 예외를 사유와 재시도 가능 여부로 옮깁니다."""

    detail, retryable = classify_error(exc)
    return {"job_id": job_id, "detail": detail, "retryable": retryable}


def classify_error(exc: Exception) -> tuple[str, bool]:
    """예외를 (사유, 재시도 가능 여부) 로 옮깁니다.

    retryable 은 "같은 요청을 그대로 다시 보내볼 만한가" 입니다. 분석 서버는 이 값이
    True 면 일시적 장애로, False 면 요청 자체가 성립하지 않은 것으로 다룹니다.

    - 네트워크·타임아웃: 플랫폼이 잠깐 느리거나 막은 경우라 다시 시도할 만합니다 -> True
    - NotSupportedError: 이 플랫폼이 리뷰 수집을 아예 제공하지 않습니다 -> False
    - MissingCredentialError: 설정을 채우기 전에는 몇 번을 보내도 같습니다 -> False
    - ParseError 등 CollectorError: 사이트 개편으로 파싱이 깨진 경우입니다.
      코드를 고치기 전에는 재시도해도 같습니다 -> False
    - 그 밖의 예외: 원인을 모르는 버그입니다. 재시도를 권하면 같은 실패만 반복시키므로 -> False
    """

    if isinstance(exc, httpx.TimeoutException):
        return f"수집 중 응답이 오지 않았습니다: {exc}", True
    if isinstance(exc, httpx.HTTPError):
        return f"수집 중 통신에 실패했습니다: {type(exc).__name__}: {exc}", True
    if isinstance(exc, NotSupportedError):
        return str(exc), False
    if isinstance(exc, MissingCredentialError):
        return str(exc), False
    if isinstance(exc, CollectorError):
        return str(exc), False
    return f"{type(exc).__name__}: {exc}", False


def parse_last_event_id(raw: str | None) -> int:
    """`Last-Event-ID` 헤더를 "이미 받은 리뷰 수" 로 읽습니다.

    review 이벤트의 id 로 1부터의 일련번호를 쓰기 때문에, 마지막으로 받은 id 가 곧
    이미 받은 개수입니다. 재연결 시 그만큼 건너뛰고 이어서 보냅니다.

    이 이어보내기는 최선 노력입니다. 같은 상품·같은 limit 이면 수집 순서가 같다는
    가정에 기대고 있습니다. 순서가 흔들리면 일부 리뷰를 건너뛸 수 있으므로,
    헤더를 해석할 수 없으면 건너뛰지 않고 처음부터 다시 보냅니다.
    """

    if raw is None:
        return 0
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        return 0
    return value if value > 0 else 0
