"""SQS 메시지 계약.

``LlmJobRequest``는 Spring이 발행하는 실제 계약 v1이다. 완료 통보는 응답 큐가
아닌 HTTP 콜백으로 보낸다. 아래의 ``Queue*Envelope``는 이전 독립 HTTP 경로의
호환 코드라 SQS 소비에는 사용하지 않는다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class LlmJobRequest(BaseModel):
    """Spring이 LLM 요청 큐에 발행하는 계약 v1 (`docs/AI_CONTRACT.md`)."""

    contractVersion: Literal[1]
    requestId: str
    jobType: Literal["TERM_EXTRACTION", "DOCUMENT_CHECK"]
    jobId: int
    workspaceId: int
    dictionaryId: int | None
    sourceDocumentIds: list[int] | None
    documentId: int | None
    documentVersionNo: int | None
    mode: Literal["STUB", "MOCK", "REAL"]
    requestedAt: str


class QueueTaskEnvelope(BaseModel):
    """요청 큐(`SQS_REQUEST_QUEUE_URL`)에서 받는 메시지 본문(초안)."""

    jobId: str
    type: Literal["extract", "contrast"]
    # 이전 독립 워커 경로의 응답 큐 URL이다. Spring LLM 계약에서는 쓰지 않는다.
    replyQueueUrl: str | None = None
    # type이 "extract"면 ExtractJobRequest, "contrast"면 ContrastJobRequest 형태의 JSON.
    payload: dict


class QueueResultEnvelope(BaseModel):
    """응답 큐로 발행하는 메시지 본문(초안)."""

    jobId: str
    type: Literal["extract", "contrast"]
    status: Literal["SUCCESS", "FAILED"]
    # 요청에 실려온 accessToken을 검증 없이 그대로 돌려준다 — 백엔드가 필요하면
    # 자기 쪽에서 쓸 수 있게(2026-09-14, app/job_schema.py 참고).
    accessToken: str | None = None
    # SUCCESS면 ExtractResponse/ContrastResponse 형태의 JSON, FAILED면 None.
    result: dict | None = None
    error: str | None = None
