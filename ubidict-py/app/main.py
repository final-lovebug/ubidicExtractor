"""FastAPI 앱 — `test/`에서 검증된 파이프라인을 실제로 호출하는 프로덕션 진입점.

`test/app/main.py`(§8 1단계 "계약 목킹")와 달리 여기는 고정 응답이 아니라
`app.service`(실제 파이프라인)를 호출한다.

**엔드포인트 두 세대가 같이 있다**:
- `/extract`·`/contrast`(문서를 인라인으로 통째로 받는 옛 계약, §3/§12) —
  직접 테스트·관리자 확인용으로 남겨둔다. fixtures로 바로 찔러볼 때 편하다.
- `/jobs/extract`·`/jobs/contrast`(2026-09-14 신설, `documentIds`/
  `dictionaryId`만 받아 백엔드 DB에서 직접 읽는다) — **이게 실제 백엔드가
  쓰게 될 경로**. `queue_consumer.py`도 SQS로 받으면 결국 같은
  `run_extract_job`/`run_contrast_job`을 부른다 — HTTP·SQS 어느 쪽으로
  들어와도 로직은 하나다.

SQS 컨슈머는 `lifespan`에서 백그라운드 태스크로 시작한다 — `.env`에
`SQS_REQUEST_QUEUE_URL`이 없으면 컨슈머는 조용히 시작을 건너뛴다(로컬
HTTP 전용 개발 가능).
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException

from app.job_schema import ContrastJobRequest, ExtractJobRequest
from app.pipeline.model_chain import load_default_model_chain
from app.queue_consumer import start_consumer_loop, stop_consumer_loop
from app.queue_schema import QueueResultEnvelope
from app.schema import ContrastRequest, ContrastResponse, ExtractRequest, ExtractResponse, HealthResponse
from app.service import run_contrast, run_contrast_job, run_extract, run_extract_job

load_dotenv()

# CloudWatch(표준출력)로 나갈 로그 레벨. usage_log.py는 print()로 별도 구조화
# JSON을 찍으므로 이 설정과 무관하게 항상 보인다 — 이건 queue_consumer.py 등의
# logger.info/warning/exception이 실제로 출력되게 하기 위한 것.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

_consumer_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _consumer_task
    _consumer_task = asyncio.create_task(start_consumer_loop())
    yield
    stop_consumer_loop()
    if _consumer_task:
        _consumer_task.cancel()


app = FastAPI(title="ubidict-py", lifespan=lifespan)


@app.post("/extract", response_model=ExtractResponse)
def extract(request: ExtractRequest) -> ExtractResponse:
    try:
        return run_extract(request)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.post("/contrast", response_model=ContrastResponse)
def contrast(request: ContrastRequest) -> ContrastResponse:
    try:
        return run_contrast(request)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.post("/jobs/extract", response_model=QueueResultEnvelope)
def extract_job(job: ExtractJobRequest) -> QueueResultEnvelope:
    """`documentIds`/`dictionaryId`로 백엔드 DB에서 직접 읽어 처리한다 — 실제
    경로용(§ 위 모듈 docstring). `queue_consumer.py`의 SQS 처리와 같은
    `run_extract_job`을 부르므로 결과 형태(`QueueResultEnvelope`)도 맞췄다."""
    try:
        response = run_extract_job(job)
        return QueueResultEnvelope(
            jobId=job.jobId, type="extract", status="SUCCESS", accessToken=job.accessToken,
            result=response.model_dump(), error=None,
        )
    except Exception as e:
        return QueueResultEnvelope(
            jobId=job.jobId, type="extract", status="FAILED", accessToken=job.accessToken,
            result=None, error=str(e),
        )


@app.post("/jobs/contrast", response_model=QueueResultEnvelope)
def contrast_job(job: ContrastJobRequest) -> QueueResultEnvelope:
    """`/jobs/extract`와 동일한 이유로 존재 — contrast용."""
    try:
        response = run_contrast_job(job)
        return QueueResultEnvelope(
            jobId=job.jobId, type="contrast", status="SUCCESS", accessToken=job.accessToken,
            result=response.model_dump(), error=None,
        )
    except Exception as e:
        return QueueResultEnvelope(
            jobId=job.jobId, type="contrast", status="FAILED", accessToken=job.accessToken,
            result=None, error=str(e),
        )


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    chain = load_default_model_chain()
    return HealthResponse(status="ok", model=chain[0] if chain else os.getenv("GEMINI_MODEL", "unset"))
