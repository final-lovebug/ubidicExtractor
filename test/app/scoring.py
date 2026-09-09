"""SPEC.md §7 "채점 출력"을 계산하는 로직.

`gold.csv` 헤더는 §9b 그대로다: `id,kind,forms,preferred_form_hint,english_hint,trap_type,note`
(fixtures/README.md에 남아있는 `must_appear_in`은 옛 컬럼이라 여기서는 쓰지 않는다).

지금(2단계) 시점에는 실제 후보가 1단계 예시 2개뿐이라 숫자가 낮게 나오는 게
정상이다 — 3~5단계에서 VARIANT·SYNONYM·HOMOGRAPH 판정 로직이 붙으면서 올라간다.
이 모듈의 목적은 "채점 계산 자체가 맞는가"이지 "지금 숫자가 목표치를 넘는가"가
아니다.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from app.schema import Candidate, ExtractResponse


@dataclass
class GoldRow:
    id: str
    kind: str  # VARIANT | SYNONYM | HOMOGRAPH | NEGATIVE
    forms: list[str]
    preferred_form_hint: str
    english_hint: str
    trap_type: str
    note: str


def load_gold(gold_path: Path) -> list[GoldRow]:
    rows: list[GoldRow] = []
    with gold_path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            rows.append(
                GoldRow(
                    id=r["id"],
                    kind=r["kind"],
                    forms=[form.strip() for form in r["forms"].split("|") if form.strip()],
                    preferred_form_hint=r["preferred_form_hint"],
                    english_hint=r["english_hint"],
                    trap_type=r["trap_type"],
                    note=r["note"],
                )
            )
    return rows


@dataclass
class RowResult:
    row: GoldRow
    status: str  # 완전 일치 | 부분 일치 | 누락 | 오탐
    missing: list[str] = field(default_factory=list)
    matched_candidate_id: str | None = None


@dataclass
class ScoreReport:
    group_results: list[RowResult]
    homograph_results: list[RowResult]
    false_positives: list[RowResult]
    group_precision: float | None
    group_recall: float | None


def _best_overlap(forms: set[str], candidates: list[Candidate]) -> tuple[Candidate | None, set[str]]:
    best: Candidate | None = None
    best_inter: set[str] = set()
    for c in candidates:
        inter = forms & set(c.forms)
        if len(inter) > len(best_inter):
            best, best_inter = c, inter
    return best, best_inter


def compute_score(response: ExtractResponse, gold_rows: list[GoldRow]) -> ScoreReport:
    candidates = response.candidates
    group_candidates = [c for c in candidates if c.kind in ("VARIANT", "SYNONYM")]
    homograph_candidates = [c for c in candidates if c.kind == "HOMOGRAPH"]

    group_results: list[RowResult] = []
    homograph_results: list[RowResult] = []
    false_positives: list[RowResult] = []

    for row in gold_rows:
        forms = set(row.forms)

        if row.kind == "NEGATIVE":
            # 오탐 = 후보가 함정의 forms를 "전부" 묶어냈을 때만. 그냥 겹치는 것과는
            # 다르다 — 예: N3(결제/결제 수단)에 대해, "결제 수단"과 "결제수단"만
            # 묶은 정상적인 VARIANT 후보(G5)는 "결제"를 포함하지 않으므로 오탐이
            # 아니다. 부분 겹침으로 판정하면 정상 후보가 함정에 오인된다.
            hit = next((c for c in candidates if forms <= set(c.forms)), None)
            if hit is not None:
                false_positives.append(RowResult(row=row, status="오탐", matched_candidate_id=hit.candidateId))
            continue

        pool = homograph_candidates if row.kind == "HOMOGRAPH" else group_candidates
        bucket = homograph_results if row.kind == "HOMOGRAPH" else group_results
        cand, inter = _best_overlap(forms, pool)

        if not inter:
            bucket.append(RowResult(row=row, status="누락", missing=sorted(forms)))
        elif inter == forms:
            bucket.append(RowResult(row=row, status="완전 일치", matched_candidate_id=cand.candidateId))
        else:
            bucket.append(
                RowResult(
                    row=row,
                    status="부분 일치",
                    missing=sorted(forms - inter),
                    matched_candidate_id=cand.candidateId,
                )
            )

    found = sum(1 for r in group_results if r.status in ("완전 일치", "부분 일치"))
    fp = len(false_positives)
    total_group = len(group_results)

    precision = found / (found + fp) if (found + fp) else None
    recall = found / total_group if total_group else None

    return ScoreReport(
        group_results=group_results,
        homograph_results=homograph_results,
        false_positives=false_positives,
        group_precision=precision,
        group_recall=recall,
    )


def format_report(report: ScoreReport) -> str:
    lines: list[str] = []

    def _ids(results: list[RowResult], status: str) -> str:
        return " ".join(r.row.id for r in results if r.status == status)

    total_group = len(report.group_results)
    complete = sum(1 for r in report.group_results if r.status == "완전 일치")
    partial = [r for r in report.group_results if r.status == "부분 일치"]
    missing = sum(1 for r in report.group_results if r.status == "누락")

    lines.append("=== 그룹형 (VARIANT · SYNONYM) ===")
    lines.append(f"완전 일치   {complete} / {total_group}     {_ids(report.group_results, '완전 일치')}")
    for r in partial:
        lines.append(f"부분 일치   {r.row.id}  (누락: {', '.join(r.missing)})")
    lines.append(f"누락        {missing} / {total_group}     {_ids(report.group_results, '누락')}")
    lines.append(f"오탐        {len(report.false_positives)}")
    for fp in report.false_positives:
        lines.append(f"            {' + '.join(fp.row.forms)}  ({fp.row.trap_type})")
    lines.append("")
    p = f"{report.group_precision:.2f}" if report.group_precision is not None else "N/A"
    r = f"{report.group_recall:.2f}" if report.group_recall is not None else "N/A"
    pass_mark = "✓" if (report.group_precision or 0) >= 0.70 else "✗"
    lines.append(f"정밀도  {p}   (합격선 0.70)   {pass_mark}")
    lines.append(f"재현율  {r}")

    lines.append("")
    total_homograph = len(report.homograph_results)
    h_complete = sum(1 for r in report.homograph_results if r.status == "완전 일치")
    h_missing = sum(1 for r in report.homograph_results if r.status == "누락")
    lines.append("=== 갈래형 (HOMOGRAPH) ===")
    lines.append(f"완전 일치   {h_complete} / {total_homograph}     {_ids(report.homograph_results, '완전 일치')}")
    if h_missing:
        missing_ids = _ids(report.homograph_results, "누락")
        lines.append(f"누락        {h_missing} / {total_homograph}     {missing_ids}")

    return "\n".join(lines)
