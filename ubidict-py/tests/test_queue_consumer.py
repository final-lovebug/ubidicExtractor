"""app.queue_consumer._handle_message 단위 테스트 — boto3·MySQL·백엔드 DB·
Gemini 전부 모킹한다. 실제 SQS 통합 확인은 2026-09-13에 실제 AWS 큐로 이미
했다(task.md 참고, 단 그때는 옛 인라인 문서 계약 기준 — 2026-09-14
documentIds/dictionaryId 기반으로 바뀐 뒤에는 아직 실제 SQS로 재확인 안 함).
여기서는 그 흐름을 만드는 분기 로직 자체를 회귀 테스트로 고정한다.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from app.queue_consumer import _handle_message

_QUEUE_URL = "https://sqs.example/queue"


def _sqs_message(body: dict | str, receipt_handle: str = "rh-1") -> dict:
    return {
        "Body": body if isinstance(body, str) else json.dumps(body),
        "ReceiptHandle": receipt_handle,
    }


def _extract_envelope(job_id: str = "job-1", access_token: str = "tok-1") -> dict:
    return {
        "jobId": job_id,
        "type": "extract",
        "replyQueueUrl": "https://sqs.example/reply",
        "payload": {
            "jobId": job_id,
            "workspaceId": 1,
            "dictionaryVersionNo": 1,
            "dictionaryId": 1,
            "documentIds": [1, 2],
            "accessToken": access_token,
            "mode": "real",
        },
    }


def test_malformed_message_is_not_deleted():
    client = MagicMock()
    message = _sqs_message("이건 JSON이 아니다")

    _handle_message(client, _QUEUE_URL, message)

    client.delete_message.assert_not_called()


@patch("app.queue_consumer.already_processed", return_value=True)
def test_duplicate_job_id_skips_processing_and_deletes(mock_already_processed):
    client = MagicMock()
    message = _sqs_message(_extract_envelope("dup-job"))

    with patch("app.queue_consumer.run_extract_job") as mock_run_extract_job:
        _handle_message(client, _QUEUE_URL, message)
        mock_run_extract_job.assert_not_called()

    mock_already_processed.assert_called_once_with("dup-job")
    client.delete_message.assert_called_once_with(QueueUrl=_QUEUE_URL, ReceiptHandle="rh-1")


@patch("app.queue_consumer.already_processed", side_effect=RuntimeError("MySQL 연결 불가"))
def test_idempotency_check_failure_leaves_message_undeleted(_mock_already_processed):
    """멱등성 확인 자체가 실패하면(DB 다운 등) 유실보다 중복 재시도가 낫다 — 삭제 안 함."""
    client = MagicMock()
    message = _sqs_message(_extract_envelope())

    _handle_message(client, _QUEUE_URL, message)

    client.delete_message.assert_not_called()
    client.send_message.assert_not_called()


@patch("app.queue_consumer.mark_processed")
@patch("app.queue_consumer.already_processed", return_value=False)
def test_successful_extract_publishes_reply_marks_and_deletes(_mock_already_processed, mock_mark_processed):
    client = MagicMock()
    message = _sqs_message(_extract_envelope("ok-job", access_token="secret-tok"))

    fake_response = MagicMock()
    fake_response.model_dump.return_value = {"jobId": "ok-job", "status": "SUCCESS"}

    with patch("app.queue_consumer.run_extract_job", return_value=fake_response) as mock_run_extract_job:
        _handle_message(client, _QUEUE_URL, message)

    mock_run_extract_job.assert_called_once()
    job_arg = mock_run_extract_job.call_args.args[0]
    assert job_arg.jobId == "ok-job"
    assert job_arg.documentIds == [1, 2]

    # 응답 큐로 SUCCESS 봉투가 발행됐는지, accessToken이 그대로 돌아오는지
    client.send_message.assert_called_once()
    sent_kwargs = client.send_message.call_args.kwargs
    assert sent_kwargs["QueueUrl"] == "https://sqs.example/reply"
    sent_body = json.loads(sent_kwargs["MessageBody"])
    assert sent_body["status"] == "SUCCESS"
    assert sent_body["jobId"] == "ok-job"
    assert sent_body["accessToken"] == "secret-tok"

    mock_mark_processed.assert_called_once_with("ok-job", "extract")
    client.delete_message.assert_called_once_with(QueueUrl=_QUEUE_URL, ReceiptHandle="rh-1")


@patch("app.queue_consumer.mark_processed")
@patch("app.queue_consumer.already_processed", return_value=False)
def test_processing_failure_publishes_failed_reply_but_does_not_delete(_mock_already_processed, mock_mark_processed):
    """처리(DB 조회·Gemini 호출 등) 실패 — FAILED 응답(+accessToken 에코)은 보내되,
    멱등성 기록·메시지 삭제는 안 한다(SQS 재시도에 맡긴다)."""
    client = MagicMock()
    message = _sqs_message(_extract_envelope("fail-job", access_token="secret-tok"))

    with patch("app.queue_consumer.run_extract_job", side_effect=RuntimeError("DB 조회 실패")):
        _handle_message(client, _QUEUE_URL, message)

    client.send_message.assert_called_once()
    sent_body = json.loads(client.send_message.call_args.kwargs["MessageBody"])
    assert sent_body["status"] == "FAILED"
    assert sent_body["error"] == "DB 조회 실패"
    assert sent_body["accessToken"] == "secret-tok"  # 실패해도 파싱까지는 됐으니 에코된다

    mock_mark_processed.assert_not_called()
    client.delete_message.assert_not_called()


@patch("app.queue_consumer.already_processed", return_value=False)
def test_missing_reply_queue_url_logs_and_does_not_crash(_mock_already_processed):
    """envelope에 replyQueueUrl도 없고 .env의 SQS_REPLY_QUEUE_URL도 없으면
    응답을 못 보내지만 예외로 죽지는 않는다."""
    client = MagicMock()
    envelope = _extract_envelope("no-reply-job")
    envelope["replyQueueUrl"] = None
    message = _sqs_message(envelope)

    fake_response = MagicMock()
    fake_response.model_dump.return_value = {"jobId": "no-reply-job", "status": "SUCCESS"}

    with patch.dict("os.environ", {}, clear=False):
        import os

        os.environ.pop("SQS_REPLY_QUEUE_URL", None)
        with patch("app.queue_consumer.run_extract_job", return_value=fake_response):
            _handle_message(client, _QUEUE_URL, message)

    client.send_message.assert_not_called()


@patch("app.queue_consumer.mark_processed")
@patch("app.queue_consumer.already_processed", return_value=False)
def test_contrast_type_calls_run_contrast_job_not_run_extract_job(_mock_already_processed, _mock_mark_processed):
    client = MagicMock()
    envelope = _extract_envelope("contrast-job")
    envelope["type"] = "contrast"
    message = _sqs_message(envelope)

    fake_response = MagicMock()
    fake_response.model_dump.return_value = {"jobId": "contrast-job", "status": "SUCCESS"}

    with patch("app.queue_consumer.run_contrast_job", return_value=fake_response) as mock_run_contrast_job, \
         patch("app.queue_consumer.run_extract_job") as mock_run_extract_job:
        _handle_message(client, _QUEUE_URL, message)
        mock_run_contrast_job.assert_called_once()
        mock_run_extract_job.assert_not_called()
