"""SPEC.md §4 — VARIANT 판정. LLM을 쓰지 않는다.

**후보 표기(form)를 문서에서 어떻게 찾아내는가.** 마크다운에서 강조된 스팬
(`**볼드**`, `` `코드` ``)을 후보 표기로 본다 — 문서 작성자가 강조·코드로
표시해 둔 곳이 대체로 용어이기 때문이다. `fixtures/docs/*.md`를 실제로 읽어보면
이 프로젝트의 핵심 용어가 거의 다 이 두 방식 중 하나로 표시돼 있다.
형태소 분석기·임베딩 클러스터링은 쓰지 않는다 (AGENTS.md 금지 항목).

이 방식이 완벽하지는 않다 — 가끔 문장 전체가 볼드로 묶인 경우("**주문이
생성된다.**")도 후보로 잡힌다. 하지만 그런 건 다른 표기와 정규화 결과가
같을 일이 없어 그룹이 1개짜리로 끝나고(= "변형"이 아니므로) 자동으로
버려진다 — 오탐으로 이어지지 않는다.

**정규화 규칙(§4, 편집거리 없음 — §10 D-1 참고):**
    공백 제거 → 하이픈·언더스코어 제거 → 소문자화 → 조사 제거
    (조사를 떼어 길이가 2 미만이 되면 떼지 않는다)

정규화 결과가 완전히 같은 표기끼리만 그룹으로 묶는다. `existingTerms`에
이미 등재된 표기가 섞인 그룹은 후보로 내지 않는다(§4 규칙4) — 이 필터는
4단계에서 SYNONYM에도 똑같이 필요해지면 `pipeline/postprocess.py`로 옮긴다.
"""

from __future__ import annotations

import re

from app.pipeline.normalize import count_all, find_occurrences, make_snippet, round_robin_sample
from app.schema import ExistingTerm, ExtractRequest, GroupCandidate, Occurrence

# "으로"가 "로"보다 먼저 검사돼야 한다 — 아니면 "결제카드로" 같은 말에서
# "로"만 떼고 "으"가 어중간하게 남는 식의 오분석이 생긴다.
_PARTICLES = ["으로", "이", "가", "은", "는", "을", "를", "의", "에", "로"]

_BOLD_RE = re.compile(r"\*\*([^*\n]+?)\*\*")
_CODE_RE = re.compile(r"`([^`\n]+?)`")


def normalize_form(text: str) -> str:
    """§4 정규화: 공백 제거 → 하이픈·언더스코어 제거 → 소문자화 → 조사 제거."""
    result = re.sub(r"\s+", "", text)
    result = result.replace("-", "").replace("_", "")
    result = result.lower()
    for particle in _PARTICLES:
        if result.endswith(particle) and len(result) - len(particle) >= 2:
            return result[: -len(particle)]
    return result


def _extract_candidate_forms(request: ExtractRequest) -> list[tuple[str, str]]:
    """마크다운 강조·코드 스팬에서 (documentId, form) 후보를 전부 추출한다."""
    hits: list[tuple[str, str]] = []
    for doc in request.documents:
        for pattern in (_BOLD_RE, _CODE_RE):
            for m in pattern.finditer(doc.content):
                form = m.group(1).strip()
                if form:
                    hits.append((doc.documentId, form))
    return hits


_HANGUL_RUN_RE = re.compile(r"[가-힣]+")


def _strip_trailing_particle(word: str) -> str:
    """`normalize_form`과 달리 소문자화·기호 제거는 안 하고 조사만 뗀다 —
    표기를 뭉개지 않고 원형 그대로 힌트에 쓰기 위해서."""
    for particle in _PARTICLES:
        if word.endswith(particle) and len(word) - len(particle) >= 2:
            return word[: -len(particle)]
    return word


def find_frequent_terms(request: ExtractRequest, min_documents: int = 2) -> list[tuple[str, int, int]]:
    """공백으로 어절을 나누고 조사를 뗀 뒤, 여러 문서에 걸쳐 반복 등장하는
    2~4음절 한글 표기를 찾는다. **마크다운 강조 여부와 무관** — 순수 텍스트
    통계라서 `_extract_candidate_forms`(볼드/코드 의존, §10 D-8 참고)와 달리
    강조 없는 평문 문서에도 그대로 적용된다.

    형태소 분석기를 쓰지 않는다(AGENTS.md 금지) — 공백 분리 + 고정 조사
    목록만으로 근사한다. 완벽한 명사 추출은 아니고 "힌트" 용도로 충분한
    수준이다.

    반환값: (표기, 총 출현 횟수, 등장 문서 수) 목록. 등장 문서 수 내림차순.
    """
    doc_sets: dict[str, set[str]] = {}
    counts: dict[str, int] = {}
    for doc in request.documents:
        for raw_word in doc.content.split():
            for run in _HANGUL_RUN_RE.findall(raw_word):
                term = _strip_trailing_particle(run)
                if not (2 <= len(term) <= 4):
                    continue
                if term.endswith("다"):
                    continue  # 한국어 용언(동사·형용사)의 기본형은 "다"로 끝난다 —
                    # "한다"/"없다"/"된다" 같은 동사를 명사 후보에서 뺀다
                counts[term] = counts.get(term, 0) + 1
                doc_sets.setdefault(term, set()).add(doc.documentId)

    results = [
        (term, counts[term], len(doc_ids)) for term, doc_ids in doc_sets.items() if len(doc_ids) >= min_documents
    ]
    results.sort(key=lambda t: (-t[2], -t[1]))
    return results


def _is_registered(form: str, existing_terms: list[ExistingTerm]) -> bool:
    return any(form == t.preferredForm or form in t.synonyms for t in existing_terms)


def find_variants(request: ExtractRequest) -> list[GroupCandidate]:
    """실제 fixtures 문서를 스캔해 VARIANT 후보를 규칙만으로 뽑는다."""
    hits = _extract_candidate_forms(request)

    # 정규화 키 → 그 키로 묶이는 고유 표기들 (처음 등장한 순서 유지)
    groups: dict[str, list[str]] = {}
    key_order: list[str] = []
    for _doc_id, form in hits:
        key = normalize_form(form)
        if not key:
            continue
        if key not in groups:
            groups[key] = []
            key_order.append(key)
        if form not in groups[key]:
            groups[key].append(form)

    candidates: list[GroupCandidate] = []
    for key in key_order:
        forms = groups[key]
        if len(forms) < 2:
            continue  # 표기가 하나뿐이면 "변형"이 아니다
        if any(_is_registered(f, request.existingTerms) for f in forms):
            continue  # §4 규칙4 — 이미 등재된 표기는 후보로 내지 않는다

        per_form_occurrences: dict[str, list[Occurrence]] = {form: [] for form in forms}
        for form in forms:
            for doc in request.documents:
                for start, end in find_occurrences(doc.content, form):
                    per_form_occurrences[form].append(
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
        # 가장 많이 쓰인 표기를 대표로. 동점이면 먼저 등장한 쪽
        preferred = max(forms, key=lambda f: (len(per_form_occurrences[f]), -forms.index(f)))
        # 라운드로빈으로 뽑아야 한다 — 앞에서부터 자르면 occurrence가 많은
        # 표기 하나가 5개를 다 채워서 다른 표기가 전혀 안 보일 수 있다
        sample_occurrences = round_robin_sample([per_form_occurrences[f] for f in forms], 5)

        candidates.append(
            GroupCandidate(
                candidateId="",  # 아래에서 일괄 부여
                kind="VARIANT",
                forms=forms,
                proposedPreferredForm=preferred,
                proposedEnglishName=None,
                proposedDefinition=f"\"{', '.join(forms)}\"는 표기만 다르고 같은 대상을 가리키는 변형이다.",
                confidence=1.0,
                reason=f"정규화(공백·하이픈·언더스코어 제거, 소문자화, 조사 제거) 결과가 모두 '{key}'로 같다.",
                occurrenceCount=occurrence_count,
                documentCount=document_count,
                occurrences=sample_occurrences,
            )
        )

    for idx, candidate in enumerate(candidates, start=1):
        candidate.candidateId = f"c-{idx}"

    return candidates
