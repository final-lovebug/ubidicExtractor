"""FastAPI 앱 — `test/`에서 검증된 파이프라인을 실제로 호출하는 프로덕션 진입점.

`test/app/main.py`(§8 1단계 "계약 목킹")와 달리 여기는 고정 응답이 아니라
`app.service`(실제 파이프라인)를 호출한다. `/extract`·`/contrast` HTTP
경로는 프로덕션 트래픽의 주경로가 아니다 — **주경로는 SQS**(`queue_consumer.py`)
다. 이 두 HTTP 경로는 직접 테스트·관리자 확인용으로 남겨둔다(같은
`service.py` 함수를 호출하므로 로직 중복은 없다).

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

from app.pipeline.model_chain import load_default_model_chain
from app.queue_consumer import start_consumer_loop, stop_consumer_loop
from app.schema import ContrastRequest, ContrastResponse, ExtractRequest, ExtractResponse, HealthResponse
from app.service import run_contrast, run_extract

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


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    chain = load_default_model_chain()
    return HealthResponse(status="ok", model=chain[0] if chain else os.getenv("GEMINI_MODEL", "unset"))
