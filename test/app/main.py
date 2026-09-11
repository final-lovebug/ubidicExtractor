"""SPEC.md §8 1단계 — 계약 목킹.

실제 판정 로직 없이 §3의 스키마와 정확히 일치하는 고정 예시 JSON을 반환한다.
Spring 팀이 이 응답으로 먼저 연동을 시작할 수 있게 하는 것이 목적이다.

2단계(문서 파싱) 이후는 아직 구현하지 않는다.

`/contrast`(SPEC.md §12, DictionaryContrast)도 같은 이유로 아직 §12.4 C-1
(계약 목킹) 단계다 — 실제 규칙 매칭(§12.4 C-2)은 `app/pipeline/contrast.py`
+ `app/cli.py`의 `contrast` 명령으로 CLI에서 먼저 검증하고, HTTP 배선은
`/extract`와 함께 나중에 한다.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from fastapi import FastAPI

from app.schema import (
    ContrastRequest,
    ContrastResponse,
    ContrastSuggestion,
    ExtractRequest,
    ExtractResponse,
    GroupCandidate,
    HealthResponse,
    HomographCandidate,
    Occurrence,
    Sense,
    Usage,
)

load_dotenv()

app = FastAPI(title="ai-service (mock)")


def _fixed_candidates() -> list[GroupCandidate | HomographCandidate]:
    """SPEC.md §3.3·§3.4의 예시를 그대로 옮긴 고정 후보 목록.

    그룹형(SYNONYM) 1개 + 갈래형(HOMOGRAPH) 1개 — 두 응답 형태 모두
    스키마 검증을 통과하는지 보이기 위해 둘 다 넣는다.
    """
    synonym_candidate = GroupCandidate(
        candidateId="c-1",
        kind="SYNONYM",
        forms=["이용 보류", "휴면 전환", "suspend_account"],
        proposedPreferredForm="구독 일시정지",
        proposedEnglishName="SubscriptionPause",
        proposedDefinition="구독을 해지하지 않고 일시적으로 중단한 상태",
        confidence=0.82,
        reason="세 표현이 모두 구독 상태를 유지한 채 이용만 중단하는 것을 가리킨다",
        occurrenceCount=23,
        documentCount=5,
        occurrences=[
            Occurrence(
                documentId="d-001",
                department="기획",
                form="이용 보류",
                snippet="결제 실패가 3회 누적되면 계정을 이용 보류 상태로 전환한다",
                charStart=412,
                charEnd=417,
            )
        ],
    )

    homograph_candidate = HomographCandidate(
        candidateId="c-9",
        kind="HOMOGRAPH",
        forms=["주문"],
        confidence=0.91,
        reason="기획 문서는 장바구니에 담긴 시점을, 개발 문서는 결제 승인 이후를 주문이라 부른다",
        occurrenceCount=47,
        documentCount=9,
        senses=[
            Sense(
                label="장바구니에 담긴 상태",
                definition="구매 의사만 표시되고 결제가 이뤄지지 않은 건",
                occurrences=[
                    Occurrence(
                        documentId="d-001",
                        department="기획",
                        form="주문",
                        snippet="사용자가 장바구니에서 주문 버튼을 누르면 결제 대기 상태가 된다",
                        charStart=88,
                        charEnd=90,
                    )
                ],
            ),
            Sense(
                label="결제가 완료된 확정 건",
                definition="결제 승인이 끝나 이행 대상이 된 건",
                occurrences=[
                    Occurrence(
                        documentId="d-002",
                        department="개발",
                        form="주문",
                        snippet="결제 승인 웹훅을 받으면 주문 상태를 CONFIRMED로 갱신한다",
                        charStart=203,
                        charEnd=205,
                    )
                ],
            ),
        ],
    )

    return [synonym_candidate, homograph_candidate]


@app.post("/extract", response_model=ExtractResponse)
def extract(request: ExtractRequest) -> ExtractResponse:
    return ExtractResponse(
        jobId=request.jobId,
        status="SUCCESS",
        dictionaryVersionNo=request.dictionaryVersionNo,
        candidates=_fixed_candidates(),
        usage=Usage(
            model=os.getenv("GEMINI_MODEL", "mock"),
            inputTokens=18422,
            outputTokens=2104,
            llmCalls=0,
            elapsedMs=0,
        ),
        warnings=[],
    )


def _fixed_contrast_suggestions() -> list[ContrastSuggestion]:
    """SPEC.md §12.2의 예시를 그대로 옮긴 고정 제안 목록 — §12.4 C-1(계약 목킹)."""
    return [
        ContrastSuggestion(
            termId="t-001",
            preferredForm="구독자",
            foundForm="이용자",
            documentId="d-101",
            department="마케팅",
            snippet="이번 달 이용자 대상 혜택 안내드립니다",
            charStart=12,
            charEnd=15,
            reason="사전집에 등재된 선호 표기는 '구독자'인데 문서에는 등재된 동의어 '이용자'로 쓰였다",
            method="rule",
        )
    ]


@app.post("/contrast", response_model=ContrastResponse)
def contrast(request: ContrastRequest) -> ContrastResponse:
    return ContrastResponse(
        jobId=request.jobId,
        status="SUCCESS",
        dictionaryVersionNo=request.dictionaryVersionNo,
        suggestions=_fixed_contrast_suggestions(),
        usage=Usage(model="rule-based", inputTokens=0, outputTokens=0, llmCalls=0, elapsedMs=0),
        warnings=[],
    )


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", model=os.getenv("GEMINI_MODEL", "mock"))
