"""Spring v1 SQS 요청을 HTTP 콜백으로 끝내는 워커 경로의 회귀 테스트."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from app.queue_consumer import _handle_message

_QUEUE_URL = "https://sqs.example/lovebug-llm-request"


def _message(body: dict) -> dict:
    return {"Body": json.dumps(body), "ReceiptHandle": "rh-1"}


def _extraction_job(mode: str = "MOCK") -> dict:
    return {
        "contractVersion": 1,
        "requestId": "0d5c6f6e-0000-4000-8000-000000000001",
        "jobType": "TERM_EXTRACTION",
        "jobId": 30,
        "workspaceId": 1,
        "dictionaryId": None,
        "sourceDocumentIds": [10, 20],
        "documentId": None,
        "documentVersionNo": None,
        "mode": mode,
        "requestedAt": "2026-09-14T10:00:00+09:00",
    }


def _check_job(mode: str = "MOCK") -> dict:
    return {
        "contractVersion": 1,
        "requestId": "0d5c6f6e-0000-4000-8000-000000000002",
        "jobType": "DOCUMENT_CHECK",
        "jobId": 40,
        "workspaceId": 1,
        "dictionaryId": None,
        "sourceDocumentIds": None,
        "documentId": 10,
        "documentVersionNo": 3,
        "mode": mode,
        "requestedAt": "2026-09-14T10:00:00+09:00",
    }


def test_malformed_message_is_not_deleted():
    client = MagicMock()

    _handle_message(client, _QUEUE_URL, {"Body": "not-json", "ReceiptHandle": "rh-1"})

    client.delete_message.assert_not_called()


@patch("app.queue_consumer._post_callback", return_value=204)
def test_mock_extraction_posts_fixed_result_and_deletes_message(mock_post_callback):
    client = MagicMock()

    _handle_message(client, _QUEUE_URL, _message(_extraction_job()))

    path, body = mock_post_callback.call_args.args
    assert path == "/api/internal/llm/extractions/30/result"
    assert body["requestId"] == "0d5c6f6e-0000-4000-8000-000000000001"
    assert body["sourceDocumentIds"] == [10, 20]
    assert body["terms"][0]["form"] == "결제"
    assert body["terms"][0]["occurredDocumentIds"] == [10]
    client.delete_message.assert_called_once_with(QueueUrl=_QUEUE_URL, ReceiptHandle="rh-1")


@patch("app.queue_consumer._post_callback", return_value=204)
def test_mock_check_posts_empty_valid_suggestions_and_deletes_message(mock_post_callback):
    client = MagicMock()

    _handle_message(client, _QUEUE_URL, _message(_check_job()))

    path, body = mock_post_callback.call_args.args
    assert path == "/api/internal/llm/checks/40/result"
    assert body == {
        "requestId": "0d5c6f6e-0000-4000-8000-000000000002",
        "documentVersionNo": 3,
        "suggestions": [],
    }
    client.delete_message.assert_called_once_with(QueueUrl=_QUEUE_URL, ReceiptHandle="rh-1")


@patch("app.queue_consumer._post_callback", return_value=503)
def test_server_error_leaves_message_for_sqs_retry(_mock_post_callback):
    client = MagicMock()

    _handle_message(client, _QUEUE_URL, _message(_extraction_job()))

    client.delete_message.assert_not_called()


@patch("app.queue_consumer._post_callback", return_value=403)
def test_client_error_deletes_message_without_retry(_mock_post_callback):
    client = MagicMock()

    _handle_message(client, _QUEUE_URL, _message(_extraction_job()))

    client.delete_message.assert_called_once_with(QueueUrl=_QUEUE_URL, ReceiptHandle="rh-1")


@patch("app.queue_consumer._post_callback", return_value=204)
def test_real_mode_reports_failure_callback_until_real_worker_is_implemented(mock_post_callback):
    client = MagicMock()

    _handle_message(client, _QUEUE_URL, _message(_extraction_job(mode="REAL")))

    path, body = mock_post_callback.call_args.args
    assert path == "/api/internal/llm/extractions/30/failure"
    assert body["code"] == "REAL_MODE_NOT_IMPLEMENTED"
    client.delete_message.assert_called_once()
