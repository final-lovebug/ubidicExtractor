"""SQS 요청 큐를 롱폴링으로 소비해 처리하고, 결과를 응답 큐로 발행한다.

`queue_schema.py`의 봉투가 **초안**이라는 걸 감안하고 읽을 것 — 백엔드
실제 계약이 오면 이 파일의 파싱·발행 부분을 다시 맞춰야 한다.

흐름: 메시지 수신 → 멱등성 확인(`app.idempotency`) → 처리(`app.service`)
→ 응답 큐 발행 → 멱등성 기록 → 메시지 삭제. 실패(파싱 실패·처리 실패)는
메시지를 삭제하지 않는다 — SQS의 재시도(가시성 타임아웃 만료 후 재배달)와
DLQ(반복 실패 시 격리)에 맡긴다. DLQ 자체의 설정(최대 수신 횟수 등)은
인프라 범위라 여기서 하지 않는다.

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
import logging
import os

import boto3

from app.idempotency import already_processed, mark_processed
from app.queue_schema import QueueResultEnvelope, QueueTaskEnvelope
from app.schema import ContrastRequest, ExtractRequest
from app.service import run_contrast, run_extract

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
        envelope = QueueTaskEnvelope.model_validate_json(message["Body"])
    except Exception:
        logger.exception("메시지 파싱 실패 — 형식이 잘못됐다. 삭제하지 않고 DLQ 정책에 맡긴다.")
        return

    try:
        if already_processed(envelope.jobId):
            logger.info(f"jobId={envelope.jobId} 이미 처리됨(중복 배달) — 건너뛰고 메시지만 삭제")
            client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)
            return
    except Exception:
        # 멱등성 확인 자체가 실패하면(예: MySQL 연결 불가) 안전한 쪽으로 —
        # 메시지를 삭제하지 않고 그대로 둔다(중복 처리 위험보다 유실 위험이 더 크다).
        logger.exception(f"jobId={envelope.jobId} 멱등성 확인 실패 — 메시지를 삭제하지 않는다")
        return

    try:
        if envelope.type == "extract":
            request = ExtractRequest.model_validate(envelope.payload)
            response = run_extract(request, note="sqs")
        else:
            request = ContrastRequest.model_validate(envelope.payload)
            response = run_contrast(request, note="sqs")
        result_envelope = QueueResultEnvelope(
            jobId=envelope.jobId, type=envelope.type, status="SUCCESS",
            result=response.model_dump(), error=None,
        )
    except Exception as e:
        logger.exception(f"jobId={envelope.jobId} 처리 실패")
        result_envelope = QueueResultEnvelope(
            jobId=envelope.jobId, type=envelope.type, status="FAILED", result=None, error=str(e),
        )
        # 실패 알림은 보내되(백엔드가 참고할 수 있게), 멱등성 기록은 안 하고
        # 메시지도 삭제하지 않는다 — SQS가 재시도하다 반복 실패하면 DLQ로 간다.
        # (주의: 재시도마다 FAILED 응답이 매번 발행될 수 있다 — 백엔드가 jobId
        # 기준으로 "가장 최근 상태"만 신뢰하도록 처리해야 한다. reference/backend
        # 참고 — 이 부분은 백엔드 팀과 확정 필요.)
        _publish_reply(client, envelope, result_envelope)
        return

    _publish_reply(client, envelope, result_envelope)
    try:
        mark_processed(envelope.jobId, envelope.type)
    except Exception:
        # 응답은 이미 나갔는데 기록만 실패한 경우 — 메시지를 지워버리면 이
        # jobId가 재배달됐을 때 멱등성 체크에 걸리지 않아 다시 처리(=응답 중복
        # 발행)될 수 있다. 그래도 메시지는 지운다 — 무한 재처리보다는 낫다는
        # 판단(§10 D-38·D-40에서 반복해온 "완벽한 보장보다 알려진 트레이드오프"
        # 원칙과 같다).
        logger.exception(f"jobId={envelope.jobId} mark_processed 실패(응답은 이미 발행됨)")
    client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)


def _publish_reply(client, envelope: QueueTaskEnvelope, result_envelope: QueueResultEnvelope) -> None:
    reply_queue_url = envelope.replyQueueUrl or os.getenv("SQS_REPLY_QUEUE_URL")
    if not reply_queue_url:
        logger.error(f"jobId={envelope.jobId} — 응답 큐 URL이 없어 결과를 보낼 수 없다(메시지·환경변수 둘 다 확인)")
        return
    client.send_message(QueueUrl=reply_queue_url, MessageBody=result_envelope.model_dump_json())
