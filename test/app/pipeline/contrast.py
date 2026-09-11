"""SPEC.md §12 — DictionaryContrast(사전집 대조).

`extract` 파이프라인(VARIANT/SYNONYM/HOMOGRAPH)과는 완전히 분리된 기능이다.
신규 용어를 찾지 않는다 — **이미 사전집(`dictionary`)에 등재된 용어가 신규
문서에서 어떻게 쓰였는지**만 찾아 치환을 제안한다.

**LLM 단독(정의 기반)이다(§10 D-22)** — 실제 서비스 DB가 사전집 항목에
`synonyms`(동의어 목록)를 저장하지 않기 때문에, 등재된 동의어 문자열을
리터럴로 스캔하던 이전의 "규칙 단계"는 제거했다. 그 로직이 의존하던
입력(`synonyms`)이 실제로는 항상 빈 배열이라 영원히 아무것도 못 찾았을
것이다. 실제 매칭 로직은 `contrast_llm.py`에 있다 — 이 모듈은 픽스처
로딩과 채점만 담당한다.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from app.schema import ContrastRequest, DictionaryEntry, DocumentInput, ContrastSuggestion


def load_contrast_fixture_request(fixtures_dir: Path) -> ContrastRequest:
    """`dictionary.json` + `docs/*.md`를 읽어 §12.1 형태의 요청으로 조립한다.

    `normalize.load_fixture_request`(extract용, `workspace.json` 읽음)와 같은
    패턴이지만 완전히 별도 픽스처(`fixtures/contrast/`)를 읽는다 — extract용
    `fixtures/workspace.json`·`gold.csv`는 건드리지 않는다(§10 D-17).
    """
    meta = json.loads((fixtures_dir / "dictionary.json").read_text(encoding="utf-8"))

    documents = [
        DocumentInput(
            documentId=doc_meta["documentId"],
            title=doc_meta["title"],
            department=doc_meta["department"],
            content=(fixtures_dir / doc_meta["file"]).read_text(encoding="utf-8"),
        )
        for doc_meta in meta["documents"]
    ]
    dictionary = [DictionaryEntry(**term) for term in meta["dictionary"]]

    return ContrastRequest(
        jobId="cli-contrast-run",
        workspaceId=meta["workspaceId"],
        dictionaryVersionNo=meta["dictionaryVersionNo"],
        dictionary=dictionary,
        documents=documents,
    )


# ── 채점 (fixtures/contrast/gold.csv 대비) ─────────────────
#
# `scoring.py`(extract 전용, VARIANT/SYNONYM/HOMOGRAPH 채점)와는 목적이 달라
# 별도로 둔다 — 여기서 세는 건 "치환 제안이 맞았는가"뿐이라 훨씬 단순하다.

@dataclass
class ContrastGoldRow:
    id: str
    term_id: str
    document_id: str
    expected_text: str  # 참고용 — 이 골드 행을 적을 때 실제로 봤던 문구. 채점에는 안 쓴다(§10 D-22)
    trap_type: str
    note: str


def load_contrast_gold(gold_path: Path) -> list[ContrastGoldRow]:
    rows: list[ContrastGoldRow] = []
    with gold_path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            rows.append(
                ContrastGoldRow(
                    id=r["id"],
                    term_id=r["term_id"],
                    document_id=r["document_id"],
                    expected_text=r["expected_text"],
                    trap_type=r["trap_type"],
                    note=r["note"],
                )
            )
    return rows


@dataclass
class ContrastRowResult:
    row: ContrastGoldRow
    status: str  # 일치 | 누락


@dataclass
class StageScore:
    found: int
    total: int
    false_positives: int
    precision: float | None
    recall: float | None


@dataclass
class ContrastScoreReport:
    results: list[ContrastRowResult]
    false_positives: list[ContrastSuggestion] = field(default_factory=list)
    score: StageScore | None = None


def _stage_score(results: list[ContrastRowResult], false_positives: list[ContrastSuggestion]) -> StageScore:
    found = sum(1 for r in results if r.status == "일치")
    fp = len(false_positives)
    total = len(results)
    precision = found / (found + fp) if (found + fp) else None
    recall = found / total if total else None
    return StageScore(found=found, total=total, false_positives=fp, precision=precision, recall=recall)


def compute_contrast_score(
    suggestions: list[ContrastSuggestion], gold_rows: list[ContrastGoldRow]
) -> ContrastScoreReport:
    """`G*` 행은 찾아야 하는 제안, `N*` 행은 **나오면 안 되는** 제안이다.

    이제 매칭 방식이 LLM 하나뿐이라(§10 D-22) `termId`+`documentId`만 맞으면
    인정한다 — LLM이 실제로 짚어내는 문구(`foundForm`)를 정확히 예측할 수는
    없어서다. `expected_text`는 채점에 쓰지 않는 참고 정보다.

    `N*` 행(트랩)은 `term_id`를 비워둔다 — "어떤 termId로 갖다 붙이든, 이 문서의
    이 문구가 제안으로 나오면 오탐"이라는 뜻이다. 사전집에 아예 없는 개념(예:
    "환불"·"등급"·"주문")에 LLM이 억지로 등재된 termId를 갖다 붙이는 환각을
    검증하는 게 주 목적이다.
    """
    results: list[ContrastRowResult] = []
    for row in gold_rows:
        if row.id.startswith("N"):
            continue
        hit = next(
            (s for s in suggestions if s.termId == row.term_id and s.documentId == row.document_id),
            None,
        )
        results.append(ContrastRowResult(row=row, status="일치" if hit is not None else "누락"))

    trap_rows = [r for r in gold_rows if r.id.startswith("N")]
    false_positives = [
        s
        for s in suggestions
        if any(s.documentId == r.document_id and s.foundForm == r.expected_text for r in trap_rows)
    ]

    return ContrastScoreReport(
        results=results,
        false_positives=false_positives,
        score=_stage_score(results, false_positives),
    )


def format_contrast_report(report: ContrastScoreReport) -> str:
    lines: list[str] = []
    missing = [r for r in report.results if r.status == "누락"]

    lines.append("=== DictionaryContrast (LLM 단독, 정의 기반, §10 D-22) ===")
    if missing:
        lines.append(f"누락        {', '.join(r.row.id for r in missing)}")
    lines.append(f"오탐        {len(report.false_positives)}건")
    for fp in report.false_positives:
        lines.append(f"            {fp.termId} · {fp.documentId} · '{fp.foundForm}'")

    lines.append("")
    s = report.score
    p = f"{s.precision:.2f}" if s.precision is not None else "N/A"
    r = f"{s.recall:.2f}" if s.recall is not None else "N/A"
    lines.append(f"일치 {s.found}/{s.total}  정밀도 {p}  재현율 {r}")

    return "\n".join(lines)
