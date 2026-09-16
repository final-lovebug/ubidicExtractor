"""SQS 요청 큐를 롱폴링으로 소비하고 Spring에 HTTP 콜백한다.

Spring 계약은 응답 큐를 쓰지 않는다. 워커는 요청 메시지의 ``requestId``를
그대로 본문에 넣어 ``/api/internal/llm/**``으로 콜백하고, 2xx·4xx면 메시지를
삭제하며 5xx·네트워크 오류만 SQS 재시도에 맡긴다.

`SQS_REQUEST_QUEUE_URL`이 `.env`에 없으면 컨슈머를 아예 시작하지 않는다
— 로컬에서 HTTP 엔드포인트(`/extract`·`/contrast`)만으로 개발·테스트할 때
AWS 자격증명이 없어도 앱이 뜨게 하기 위해서다.

`SQS_ENDPOINT_URL`을 `.env`에 채우면 실제 AWS 대신 그 주소로 접속한다 —
`docker-compose.local.yml`의 LocalStack(`http://localhost:4566`)을 가리키면
실제 AWS·백엔드 없이 로컬에서 전체 흐름을 확인할 수 있다(`scripts/`
참고). 비워두면 평소처럼 실제 AWS SQS에 접속한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import boto3

from app.queue_schema import LlmJobRequest

logger = logging.getLogger("queue_consumer")

_POLL_WAIT_SECONDS = 20  # SQS 롱폴링 최대치
_running = False


def build_sqs_client():
    """`SQS_ENDPOINT_URL`이 있으면 그쪽으로(LocalStack 등), 없으면 실제 AWS로."""
    kwargs: dict = {"region_name": os.getenv("AWS_REGION")}
    endpoint_url = os.getenv("SQS_ENDPOINT_URL")
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    return boto3.client("sqs", **kwargs)


async def start_consumer_loop() -> None:
    """FastAPI `lifespan`에서 백그라운드 태스크로 띄운다(`main.py`)."""
    global _running

    queue_url = os.getenv("SQS_REQUEST_QUEUE_URL")
    if not queue_url:
        logger.warning("SQS_REQUEST_QUEUE_URL이 없어 큐 컨슈머를 시작하지 않는다(로컬 개발 모드로 간주).")
        return

    _running = True
    client = build_sqs_client()
    loop = asyncio.get_event_loop()

    logger.info(f"SQS 컨슈머 시작: {queue_url}")
    while _running:
        try:
            response = await loop.run_in_executor(
                None,
                lambda: client.receive_message(
                    QueueUrl=queue_url,
                    MaxNumberOfMessages=1,
                    WaitTimeSeconds=_POLL_WAIT_SECONDS,
                ),
            )
        except Exception:
            logger.exception("SQS receive_message 실패 — 5초 후 재시도")
            await asyncio.sleep(5)
            continue

        for message in response.get("Messages", []):
            # 블로킹 처리(Gemini 호출·MySQL 접속 포함)라 스레드 풀에서 돌린다
            # — FastAPI 이벤트 루프를 막지 않기 위해서다.
            await loop.run_in_executor(None, _handle_message, client, queue_url, message)


def stop_consumer_loop() -> None:
    global _running
    _running = False


def _handle_message(client, queue_url: str, message: dict) -> None:
    receipt_handle = message["ReceiptHandle"]

    try:
        job = LlmJobRequest.model_validate_json(message["Body"])
    except Exception:
        logger.exception("메시지 파싱 실패 — 형식이 잘못됐다. 삭제하지 않고 DLQ 정책에 맡긴다.")
        return

    try:
        callback_path, body = _callback_for(job)
        status = _post_callback(callback_path, body)
    except Exception:
        logger.exception("jobId=%s 콜백 전송 실패 — 메시지를 삭제하지 않는다", job.jobId)
        return

    if status < 500:
        client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)
        logger.info("jobId=%s callback completed. status=%s", job.jobId, status)
    else:
        logger.warning("jobId=%s callback returned %s — SQS 재시도를 위해 메시지를 남긴다", job.jobId, status)


def _callback_for(job: LlmJobRequest) -> tuple[str, dict]:
    """요청 모드와 작업 종류에 맞는 Spring 콜백 경로·본문을 만든다."""
    if job.mode == "REAL":
        return _failure_callback(job, "REAL 모드는 아직 이 워커에 구현되지 않았습니다.", "REAL_MODE_NOT_IMPLEMENTED")

    if job.jobType == "TERM_EXTRACTION":
        if not job.sourceDocumentIds:
            return _failure_callback(job, "용어 추출 요청에 sourceDocumentIds가 없습니다.", "INVALID_REQUEST")
        terms = [] if job.mode == "STUB" else [_mock_term(job.sourceDocumentIds[0])]
        return (
            f"/api/internal/llm/extractions/{job.jobId}/result",
            {"requestId": job.requestId, "sourceDocumentIds": job.sourceDocumentIds, "terms": terms},
        )

    suggestions: list[dict] = []
    return (
        f"/api/internal/llm/checks/{job.jobId}/result",
        {"requestId": job.requestId, "documentVersionNo": job.documentVersionNo, "suggestions": suggestions},
    )


def _mock_term(document_id: int) -> dict:
    """개발 환경에서만 쓰는, Spring 검증 규격을 충족하는 고정 추출 결과다."""
    return {
        "form": "결제",
        "proposedDefinition": "재화나 용역의 대가를 지급하는 행위",
        "proposedEnglishName": "Payment",
        "occurredDocumentIds": [document_id],
        "occurrenceCount": 1,
        "contextSnippets": ["mock 용어 추출 결과입니다."],
        "variantForms": ["결제", "페이먼트"],
    }


def _failure_callback(job: LlmJobRequest, reason: str, code: str) -> tuple[str, dict]:
    resource = "extractions" if job.jobType == "TERM_EXTRACTION" else "checks"
    return (
        f"/api/internal/llm/{resource}/{job.jobId}/failure",
        {"requestId": job.requestId, "reason": reason, "code": code},
    )


def _post_callback(path: str, body: dict) -> int:
    """동기 HTTP 콜백의 상태 코드를 돌려준다. HTTP 오류도 재시도 정책 판단에 쓴다."""
    base_url = os.getenv("BACKEND_CALLBACK_BASE_URL", "http://localhost:8080").rstrip("/")
    request = Request(
        f"{base_url}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            return response.status
    except HTTPError as error:
        return error.code
