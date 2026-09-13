"""SQS 메시지 봉투 — **초안**.

백엔드(Spring) 쪽 실제 메시지 스키마를 아직 확인 못 했다(`reference/backend/`
참고). 여기 정의는 "이 정도 정보면 처리·회신이 가능하다"는 가정으로 만든
초안이고, 백엔드 코드가 오면 필드명·구조를 다시 맞출 것 — 지금 이 파일에
의존해서 백엔드 쪽 계약을 미리 확정 짓지 않는다.

`payload`/`result`를 `ExtractRequest`/`ContrastRequest`·`ExtractResponse`/
`ContrastResponse` discriminated union이 아니라 그냥 `dict`로 둔 이유: 봉투
자체(`type` 필드)를 먼저 확인해야 어느 스키마로 파싱할지 알 수 있는데,
Pydantic이 봉투 파싱 단계에서부터 payload 내부 구조까지 강제하면 "봉투는
맞는데 payload가 아직 초안과 안 맞는" 경우에도 통째로 실패한다 — 초안
단계에서는 이게 더 유연하다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class QueueTaskEnvelope(BaseModel):
    """요청 큐(`SQS_REQUEST_QUEUE_URL`)에서 받는 메시지 본문(초안)."""

    jobId: str
    type: Literal["extract", "contrast"]
    # 없으면 .env의 SQS_REPLY_QUEUE_URL을 기본값으로 쓴다(queue_consumer.py).
    replyQueueUrl: str | None = None
    # type이 "extract"면 ExtractRequest, "contrast"면 ContrastRequest 형태의 JSON.
    payload: dict


class QueueResultEnvelope(BaseModel):
    """응답 큐로 발행하는 메시지 본문(초안)."""

    jobId: str
    type: Literal["extract", "contrast"]
    status: Literal["SUCCESS", "FAILED"]
    # SUCCESS면 ExtractResponse/ContrastResponse 형태의 JSON, FAILED면 None.
    result: dict | None = None
    error: str | None = None
