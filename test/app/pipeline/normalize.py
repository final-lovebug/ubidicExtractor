"""SPEC.md §5 ① — 문서 정규화·오프셋 계산.

**D-5 결정 (SPEC.md §10).** `content`는 마크다운을 포함한 원문 그대로 취급한다.
마크다운을 제거·정규화하지 않는다 — Spring이 하이라이트할 때도 요청에 담아 보낸
`content`와 같은 문자열을 기준으로 렌더링하므로, 서버가 임의로 정규화하면
오프셋이 어긋난다. fixtures 문서를 보면 `**로그인**`처럼 강조 기호가 용어를
감싸기만 하고 내부를 가르지 않으므로, 원문 그대로 리터럴 검색해도 문제없다.

VARIANT/SYNONYM/HOMOGRAPH 판정(누가 후보인지)은 여기서 하지 않는다 — 이 모듈은
"주어진 표기(form)가 실제 문서 어디에 있는지"만 계산하는 공용 오프셋 엔진이다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

from app.schema import DocumentInput, ExistingTerm, ExtractRequest, Occurrence

T = TypeVar("T")


def load_fixture_request(fixtures_dir: Path) -> ExtractRequest:
    """`workspace.json` + `docs/*.md`를 읽어 §3.1 형태의 요청으로 조립한다."""
    workspace = json.loads((fixtures_dir / "workspace.json").read_text(encoding="utf-8"))

    documents = [
        DocumentInput(
            documentId=doc_meta["documentId"],
            title=doc_meta["title"],
            department=doc_meta["department"],
            content=(fixtures_dir / doc_meta["file"]).read_text(encoding="utf-8"),
        )
        for doc_meta in workspace["documents"]
    ]
    existing_terms = [ExistingTerm(**term) for term in workspace.get("existingTerms", [])]

    return ExtractRequest(
        jobId="cli-run",
        workspaceId=workspace["workspaceId"],
        dictionaryVersionNo=workspace["dictionaryVersionNo"],
        existingTerms=existing_terms,
        documents=documents,
    )


def find_occurrences(content: str, form: str) -> list[tuple[int, int]]:
    """`form`이 `content`에 나타나는 모든 위치. 리터럴 검색, 파이썬 문자 인덱스,
    0-based, end 미포함 — §3.5 규칙 그대로."""
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        idx = content.find(form, start)
        if idx == -1:
            break
        spans.append((idx, idx + len(form)))
        start = idx + len(form)
    return spans


def make_snippet(content: str, start: int, end: int, margin: int = 30) -> str:
    """앞뒤 `margin`자를 넘기지 않는 스니펫 — §3.5 "앞뒤 30자를 넘기지 않는다"."""
    return content[max(0, start - margin):min(len(content), end + margin)]


def locate(request: ExtractRequest, document_id: str, form: str) -> Occurrence | None:
    """특정 문서에서 `form`의 첫 실제 위치를 찾아 `Occurrence`를 만든다."""
    doc = next((d for d in request.documents if d.documentId == document_id), None)
    if doc is None:
        return None
    spans = find_occurrences(doc.content, form)
    if not spans:
        return None
    start, end = spans[0]
    return Occurrence(
        documentId=doc.documentId,
        department=doc.department,
        form=form,
        snippet=make_snippet(doc.content, start, end),
        charStart=start,
        charEnd=end,
    )


def round_robin_sample(groups: list[list[T]], limit: int) -> list[T]:
    """그룹별 리스트를 라운드로빈으로 섞어 최대 `limit`개를 뽑는다.

    §3.5 "occurrences 배열은 최대 5개만 담는다"를 적용할 때, 특정 표기(form)의
    occurrence 수가 많다고 그것만으로 채워지고 다른 표기가 하나도 안 보이는 걸
    막는다 — 예: "로그인"이 8번, "로그 인"이 2번 나오면 앞에서부터 5개만 자르는
    방식은 "로그 인"을 전혀 보여주지 못한다.
    """
    sample: list[T] = []
    i = 0
    while len(sample) < limit:
        added_any = False
        for group in groups:
            if i < len(group):
                sample.append(group[i])
                added_any = True
                if len(sample) == limit:
                    return sample
        if not added_any:
            break
        i += 1
    return sample


def count_all(request: ExtractRequest, forms: list[str]) -> tuple[int, int]:
    """`forms` 중 아무거나가 전체 문서에서 나타나는 총 횟수와, 나타난 문서 수."""
    total = 0
    doc_ids: set[str] = set()
    for doc in request.documents:
        for form in forms:
            spans = find_occurrences(doc.content, form)
            if spans:
                doc_ids.add(doc.documentId)
            total += len(spans)
    return total, len(doc_ids)
