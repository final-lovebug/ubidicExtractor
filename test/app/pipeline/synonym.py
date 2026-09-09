"""LLM 판정 결과(`pipeline/llm.py`)를 §3 Candidate로 변환한다.

LLM은 forms(문자열)·판정 근거만 준다. 정확한 charStart/charEnd/snippet은
여기서 `pipeline/normalize.py`의 오프셋 엔진으로 다시 계산한다 — LLM에게
글자 위치를 맞히라고 시키지 않는다(3단계 VARIANT와 같은 원칙).

후처리(§5 ④): existingTerms 제외 · confidence 필터(0.5 미만 제외) · senses
2개 미만인 HOMOGRAPH 제외. VARIANT처럼 아직 별도 `postprocess.py`로 빼지
않고 여기 inline으로 둔다 — 두 곳(VARIANT·SYNONYM)에서 같은 모양의 필터가
반복되면 그때 공용 모듈로 옮긴다.
"""

from __future__ import annotations

from app.pipeline.llm import LlmExtractionResult
from app.pipeline.normalize import count_all, find_occurrences, locate, make_snippet, round_robin_sample
from app.schema import ExistingTerm, ExtractRequest, GroupCandidate, HomographCandidate, Occurrence, Sense

MIN_CONFIDENCE = 0.5


def _is_registered(form: str, existing_terms: list[ExistingTerm]) -> bool:
    return any(form == t.preferredForm or form in t.synonyms for t in existing_terms)


def _occurrences_for_forms(
    request: ExtractRequest, forms: list[str]
) -> tuple[list[Occurrence], int, int]:
    per_form: dict[str, list[Occurrence]] = {form: [] for form in forms}
    for form in forms:
        for doc in request.documents:
            for start, end in find_occurrences(doc.content, form):
                per_form[form].append(
                    Occurrence(
                        documentId=doc.documentId,
                        department=doc.department,
                        form=form,
                        snippet=make_snippet(doc.content, start, end),
                        charStart=start,
                        charEnd=end,
                    )
                )
    occurrence_count, document_count = count_all(request, forms)
    sample = round_robin_sample([per_form[f] for f in forms], 5)
    return sample, occurrence_count, document_count


def build_synonym_candidates(
    request: ExtractRequest, llm_result: LlmExtractionResult, start_id: int = 1
) -> list[GroupCandidate]:
    candidates: list[GroupCandidate] = []
    next_id = start_id
    for group in llm_result.synonymGroups:
        if group.confidence < MIN_CONFIDENCE:
            continue
        if any(_is_registered(f, request.existingTerms) for f in group.forms):
            continue

        occurrences, occurrence_count, document_count = _occurrences_for_forms(request, group.forms)

        candidates.append(
            GroupCandidate(
                candidateId=f"c-{next_id}",
                kind="SYNONYM",
                forms=group.forms,
                proposedPreferredForm=group.proposedPreferredForm,
                proposedEnglishName=group.proposedEnglishName,
                proposedDefinition=group.proposedDefinition,
                confidence=group.confidence,
                reason=group.reason,
                occurrenceCount=occurrence_count,
                documentCount=document_count,
                occurrences=occurrences,
            )
        )
        next_id += 1
    return candidates


def build_homograph_candidates(
    request: ExtractRequest, llm_result: LlmExtractionResult, start_id: int = 1
) -> list[HomographCandidate]:
    candidates: list[HomographCandidate] = []
    next_id = start_id
    for h in llm_result.homographs:
        if h.confidence < MIN_CONFIDENCE:
            continue
        if _is_registered(h.form, request.existingTerms):
            continue

        senses: list[Sense] = []
        for sense in h.senses:
            sense_occurrences = [
                occ for occ in (locate(request, doc_id, h.form) for doc_id in sense.documentIds) if occ
            ]
            if sense_occurrences:
                senses.append(Sense(label=sense.label, definition=sense.definition, occurrences=sense_occurrences[:5]))

        if len(senses) < 2:
            continue  # §3.4 — senses는 2개 이상이어야 후보다

        occurrence_count, document_count = count_all(request, [h.form])
        candidates.append(
            HomographCandidate(
                candidateId=f"c-{next_id}",
                kind="HOMOGRAPH",
                forms=[h.form],
                confidence=h.confidence,
                reason=h.reason,
                occurrenceCount=occurrence_count,
                documentCount=document_count,
                senses=senses,
            )
        )
        next_id += 1
    return candidates
