"""1단계(계약 목킹, `app/main.py`)에서 하드코딩했던 예시 후보 2개를 재사용하되,
occurrence의 charStart/charEnd/snippet만 `normalize.locate()`로 실제 fixtures
문서를 스캔해 계산한 값으로 채운다.

**범위.** 어떤 표기가 후보인지 정하는 판정 로직(VARIANT 규칙·LLM)은 3~5단계의
몫이라 여기서는 만들지 않는다. 이 모듈은 1단계에서 이미 정한 예시 후보 2개에
대해서만 "그 표기가 실제로 어디 있는지"를 실측한다 — 2단계(오프셋 인프라)
검증용 실물을 만드는 것이 목적이다. `app/main.py`의 HTTP 목업은 이미 Spring에
전달됐으므로 건드리지 않는다.
"""

from __future__ import annotations

from app.pipeline.normalize import count_all, locate
from app.schema import ExtractRequest, GroupCandidate, HomographCandidate, Sense

_SYNONYM_FORMS = ["이용 보류", "휴면 전환", "suspend_account"]
# (documentId, form) — 각 표기가 실제로 나타나는 문서 한 곳씩 골랐다
_SYNONYM_HITS = [
    ("d-001", "이용 보류"),
    ("d-003", "휴면 전환"),
    ("d-002", "suspend_account"),
]

_HOMOGRAPH_FORM = "주문"
# (뜻 라벨, 정의, 그 뜻으로 쓰인 문서)
_HOMOGRAPH_SENSES = [
    ("장바구니에 담긴 상태", "구매 의사만 표시되고 결제가 이뤄지지 않은 건", "d-001"),
    ("결제가 완료된 확정 건", "결제 승인이 끝나 이행 대상이 된 건", "d-002"),
]


def _build_synonym_candidate(request: ExtractRequest) -> GroupCandidate:
    occurrences = [
        occ
        for occ in (locate(request, doc_id, form) for doc_id, form in _SYNONYM_HITS)
        if occ is not None
    ]
    occurrence_count, document_count = count_all(request, _SYNONYM_FORMS)
    return GroupCandidate(
        candidateId="c-1",
        kind="SYNONYM",
        forms=_SYNONYM_FORMS,
        proposedPreferredForm="구독 일시정지",
        proposedEnglishName="SubscriptionPause",
        proposedDefinition="구독을 해지하지 않고 일시적으로 중단한 상태",
        confidence=0.82,
        reason="세 표현이 모두 구독 상태를 유지한 채 이용만 중단하는 것을 가리킨다",
        occurrenceCount=occurrence_count,
        documentCount=document_count,
        occurrences=occurrences,
    )


def _build_homograph_candidate(request: ExtractRequest) -> HomographCandidate:
    senses = []
    for label, definition, doc_id in _HOMOGRAPH_SENSES:
        occ = locate(request, doc_id, _HOMOGRAPH_FORM)
        senses.append(Sense(label=label, definition=definition, occurrences=[occ] if occ else []))
    occurrence_count, document_count = count_all(request, [_HOMOGRAPH_FORM])
    return HomographCandidate(
        candidateId="c-9",
        kind="HOMOGRAPH",
        forms=[_HOMOGRAPH_FORM],
        confidence=0.91,
        reason="기획 문서는 장바구니에 담긴 시점을, 개발 문서는 결제 승인 이후를 주문이라 부른다",
        occurrenceCount=occurrence_count,
        documentCount=document_count,
        senses=senses,
    )


def build_example_candidates(
    request: ExtractRequest,
) -> list[GroupCandidate | HomographCandidate]:
    return [_build_synonym_candidate(request), _build_homograph_candidate(request)]
