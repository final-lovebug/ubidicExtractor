"""서비스 레이어 — HTTP 라우터(`main.py`)와 SQS 컨슈머(`queue_consumer.py`)가
공유하는 단일 진입점.

`test/app/cli.py`의 `extract`/`contrast` 커맨드가 하던 일(파이프라인 호출 →
`ExtractResponse`/`ContrastResponse` 조립)과 완전히 같은 순서다 — 다른 점은
입력을 fixtures가 아니라 이미 파싱된 요청 객체(`ExtractRequest`/
`ContrastRequest`)로 받는다는 것뿐이다. 로직 자체(무엇을 호출하고 어떤
순서로 후보를 조립하는지)는 test/에서 검증된 것 그대로다.

실패하면 예외를 그대로 올려보낸다 — 여기서 삼키지 않는다. HTTP 라우터는
그걸 받아 502/500으로 응답하고, 큐 컨슈머는 그걸 받아 메시지를 삭제하지
않는다(SQS 재시도/DLQ에 맡긴다).

**`run_extract_job`/`run_contrast_job`(2026-09-14 추가)**는 문서 본문을
인라인으로 받지 않고 `documentIds`/`dictionaryId`만 받아 `app.backend_db`로
백엔드 DB에서 직접 읽어온 뒤, 위 `run_extract`/`run_contrast`에 그대로
위임한다 — 판정 로직 자체는 하나도 안 바뀐다. `accessToken`은 검증하지
않고 그대로 들고 있다가 호출부(큐 컨슈머·HTTP 라우터)가 응답에 그대로
실어 보낸다.
"""

from __future__ import annotations

import time

from app.backend_db import fetch_dictionary_entries, fetch_documents, fetch_existing_terms
from app.job_schema import ContrastJobRequest, ExtractJobRequest
from app.pipeline.contrast_llm import find_llm_contrast_matches
from app.pipeline.llm import extract_synonyms_and_homographs
from app.pipeline.synonym import build_homograph_candidates, build_synonym_candidates
from app.pipeline.variant import find_variants
from app.schema import (
    Candidate,
    ContrastRequest,
    ContrastResponse,
    ExtractRequest,
    ExtractResponse,
    Usage,
)

# stub 모드가 실제로 뭔가 하는 척(비동기 처리 시간 흉내) 기다리는 시간. 백엔드가
# 큐/폴링 배선만 검증하고 싶을 때 쓰는 값이라 정밀할 필요 없다 — 2~3초 사이.
_STUB_DELAY_SECONDS = 2.5


def run_extract(request: ExtractRequest, *, note: str = "") -> ExtractResponse:
    """§3 — VARIANT(규칙) + SYNONYM·HOMOGRAPH(LLM 1회 호출)를 모두 판정한다.

    `test/app/cli.py`의 `extract` 커맨드(90~144행)와 동일한 순서.
    """
    candidates: list[Candidate] = list(find_variants(request))

    llm_result, usage = extract_synonyms_and_homographs(request, no_cache=False, note=note)

    next_id = len(candidates) + 1
    synonym_candidates = build_synonym_candidates(request, llm_result, start_id=next_id)
    candidates.extend(synonym_candidates)
    next_id += len(synonym_candidates)
    candidates.extend(build_homograph_candidates(request, llm_result, start_id=next_id))

    return ExtractResponse(
        jobId=request.jobId,
        status="SUCCESS",
        dictionaryVersionNo=request.dictionaryVersionNo,
        candidates=candidates,
        usage=usage,
        warnings=[],
    )


def run_contrast(request: ContrastRequest, *, note: str = "") -> ContrastResponse:
    """§12 — 사전집 정의 기반 LLM 대조(§10 D-22, 규칙 단계 없음).

    `test/app/cli.py`의 `contrast` 커맨드(241~278행)와 동일한 순서.
    """
    suggestions, usage = find_llm_contrast_matches(request, no_cache=False, note=note)

    return ContrastResponse(
        jobId=request.jobId,
        status="SUCCESS",
        dictionaryVersionNo=request.dictionaryVersionNo,
        suggestions=suggestions,
        usage=usage,
        warnings=[],
    )


def run_extract_job(job: ExtractJobRequest) -> ExtractResponse:
    """`documentIds`/`dictionaryId`로 백엔드 DB에서 직접 읽어온 뒤 `run_extract`에 위임.

    `mode="stub"`이면 DB 조회·Gemini 호출 둘 다 안 하고 몇 초 뒤 빈 결과를
    돌려준다(백엔드의 `TermExtractorStub`과 같은 역할 — 큐 배선만 확인하고
    싶을 때 비용 없이 쓴다).
    """
    if job.mode == "stub":
        time.sleep(_STUB_DELAY_SECONDS)
        return ExtractResponse(
            jobId=job.jobId,
            status="SUCCESS",
            dictionaryVersionNo=job.dictionaryVersionNo,
            candidates=[],
            usage=Usage(model="stub", inputTokens=0, outputTokens=0, llmCalls=0, elapsedMs=int(_STUB_DELAY_SECONDS * 1000)),
            warnings=["mode=stub — 실제 LLM 호출 없이 빈 결과를 반환함"],
        )

    documents = fetch_documents(job.documentIds)
    existing_terms = fetch_existing_terms(job.dictionaryId)
    request = ExtractRequest(
        jobId=job.jobId,
        workspaceId=str(job.workspaceId),
        dictionaryVersionNo=job.dictionaryVersionNo,
        existingTerms=existing_terms,
        documents=documents,
    )
    return run_extract(request)


def run_contrast_job(job: ContrastJobRequest) -> ContrastResponse:
    """`documentIds`/`dictionaryId`로 백엔드 DB에서 직접 읽어온 뒤 `run_contrast`에 위임.

    `mode="stub"`이면 `run_extract_job`과 동일하게 DB·Gemini 둘 다 건너뛴다.
    """
    if job.mode == "stub":
        time.sleep(_STUB_DELAY_SECONDS)
        return ContrastResponse(
            jobId=job.jobId,
            status="SUCCESS",
            dictionaryVersionNo=job.dictionaryVersionNo,
            suggestions=[],
            usage=Usage(model="stub", inputTokens=0, outputTokens=0, llmCalls=0, elapsedMs=int(_STUB_DELAY_SECONDS * 1000)),
            warnings=["mode=stub — 실제 LLM 호출 없이 빈 결과를 반환함"],
        )

    documents = fetch_documents(job.documentIds)
    dictionary = fetch_dictionary_entries(job.dictionaryId)
    request = ContrastRequest(
        jobId=job.jobId,
        workspaceId=str(job.workspaceId),
        dictionaryVersionNo=job.dictionaryVersionNo,
        dictionary=dictionary,
        documents=documents,
    )
    return run_contrast(request)
