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
"""

from __future__ import annotations

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
)


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
