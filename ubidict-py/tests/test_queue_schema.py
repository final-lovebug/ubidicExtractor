"""app.queue_schema — 초안 봉투가 최소한 파싱은 정확히 되는지 확인.

필드 자체가 초안이라(실제 백엔드 계약 미확정, task.md 참고) 이 테스트는
"봉투 구조가 스스로 일관되는지"만 본다 — 실제 계약과의 일치는 검증 대상이 아니다.
"""

from __future__ import annotations

from app.queue_schema import LlmJobRequest, QueueResultEnvelope, QueueTaskEnvelope


def test_spring_llm_request_parses_mock_mode():
    request = LlmJobRequest.model_validate(
        {
            "contractVersion": 1,
            "requestId": "0d5c6f6e-0000-4000-8000-000000000001",
            "jobType": "TERM_EXTRACTION",
            "jobId": 30,
            "workspaceId": 1,
            "dictionaryId": None,
            "sourceDocumentIds": [10],
            "documentId": None,
            "documentVersionNo": None,
            "mode": "MOCK",
            "requestedAt": "2026-09-14T10:00:00+09:00",
        }
    )

    assert request.mode == "MOCK"
    assert request.jobType == "TERM_EXTRACTION"


def test_task_envelope_parses_minimal_extract_message():
    raw = {
        "jobId": "job-1",
        "type": "extract",
        "payload": {"documents": []},
    }
    envelope = QueueTaskEnvelope.model_validate(raw)
    assert envelope.jobId == "job-1"
    assert envelope.type == "extract"
    assert envelope.replyQueueUrl is None  # 안 주면 None — 호출부가 .env로 폴백


def test_result_envelope_round_trips_through_json():
    envelope = QueueResultEnvelope(jobId="job-2", type="contrast", status="SUCCESS", result={"a": 1}, error=None)
    restored = QueueResultEnvelope.model_validate_json(envelope.model_dump_json())
    assert restored == envelope
