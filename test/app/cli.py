"""SPEC.md §7 — HTTP보다 먼저 만드는 CLI.

    python -m app.cli extract --fixtures fixtures/ --out out/result.json
    python -m app.cli score   --result out/result.json --gold fixtures/gold.csv
    python -m app.cli verify-offsets --result out/result.json --fixtures fixtures/

`extract`는 HTTP를 거치지 않고 서비스 로직(`pipeline/`)을 직접 호출한다.
"""

from __future__ import annotations

import json
import os
import random
import shutil
from datetime import datetime, timezone
from pathlib import Path

import typer
from dotenv import load_dotenv

from app.pipeline import usage_log
from app.pipeline.llm import extract_synonyms_and_homographs
from app.pipeline.normalize import load_fixture_request, make_snippet
from app.pipeline.synonym import build_homograph_candidates, build_synonym_candidates
from app.pipeline.variant import find_variants
from app.schema import ExtractResponse, GroupCandidate, HomographCandidate, Usage
from app.scoring import compute_score, format_report, load_gold

load_dotenv()

app = typer.Typer(add_completion=False)


def _archive_to_history(result: Path, label: str = "") -> Path:
    """`result`를 손대지 않고 `out/history/`에 타임스탬프(+라벨) 붙여 복사한다.

    **덮어쓰지 않는다** — 부를 때마다 새 파일이 생긴다. `out/`는 `.gitignore`에
    있으므로 `out/history/`도 로컬에만 남고 커밋되지 않는다.
    """
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    filename = f"{timestamp}__{label}.json" if label else f"{timestamp}.json"
    history_dir = Path("out/history")
    history_dir.mkdir(parents=True, exist_ok=True)
    dest = history_dir / filename
    shutil.copy2(result, dest)
    return dest


@app.command()
def extract(
    fixtures: Path = typer.Option(Path("fixtures"), "--fixtures", help="fixtures 폴더 경로"),
    out: Path = typer.Option(Path("out/result.json"), "--out", help="결과 JSON 저장 경로"),
    model: str = typer.Option("", "--model", help="GEMINI_MODEL을 이번 실행만 덮어쓴다"),
    no_cache: bool = typer.Option(False, "--no-cache", help="LLM 응답 캐시를 무시하고 다시 호출한다"),
    skip_llm: bool = typer.Option(
        False, "--skip-llm", help="VARIANT만 돌리고 Gemini를 부르지 않는다 (오프셋 디버깅용, API 호출 안 함)"
    ),
    note: str = typer.Option("", "--note", help="튜닝 루프 메모 — usage-report 타임라인에 그대로 남는다"),
) -> None:
    """fixtures를 읽어 §3.1 요청을 조립하고, 결과를 §3.2 응답으로 저장한다.

    4단계: VARIANT(규칙) + SYNONYM·HOMOGRAPH(LLM, §5 ③처럼 한 번에 호출)를
    모두 판정한다. `--skip-llm`을 주면 VARIANT만 돌고 API를 부르지 않는다 —
    오프셋 로직만 디버깅할 때 호출을 낭비하지 않기 위해서다(AGENTS.md).

    **매번 `out/history/`에도 자동으로 스냅샷을 남긴다** — 코드를 고치기 전후
    결과를 나중에 비교할 수 있어야 하기 때문이다. 의미 있는 시점("4단계
    통과")에 이름을 붙이고 싶으면 `archive --label`을 별도로 돌린다.
    """
    request = load_fixture_request(fixtures)
    candidates: list[GroupCandidate | HomographCandidate] = list(find_variants(request))
    usage = Usage(model=model or os.getenv("GEMINI_MODEL", "mock"), inputTokens=0, outputTokens=0, llmCalls=0, elapsedMs=0)
    warnings: list[str] = []

    if skip_llm:
        warnings.append("--skip-llm로 실행됨 — SYNONYM/HOMOGRAPH는 이번 결과에 없다.")
    else:
        llm_result, usage = extract_synonyms_and_homographs(
            request, model=model or None, no_cache=no_cache, note=note
        )
        next_id = len(candidates) + 1
        synonym_candidates = build_synonym_candidates(request, llm_result, start_id=next_id)
        candidates.extend(synonym_candidates)
        next_id += len(synonym_candidates)
        candidates.extend(build_homograph_candidates(request, llm_result, start_id=next_id))

    response = ExtractResponse(
        jobId=request.jobId,
        status="SUCCESS",
        dictionaryVersionNo=request.dictionaryVersionNo,
        candidates=candidates,
        usage=usage,
        warnings=warnings,
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(response.model_dump_json(indent=2), encoding="utf-8")
    history_path = _archive_to_history(out)
    typer.echo(f"{out} 에 저장했다. candidates={len(candidates)}개")
    if not skip_llm:
        typer.echo(f"LLM 호출: model={usage.model} inputTokens={usage.inputTokens} outputTokens={usage.outputTokens}")
    typer.echo(f"이력: {history_path}")


@app.command()
def docs(
    fixtures: Path = typer.Option(Path("fixtures"), "--fixtures", help="fixtures 폴더 경로"),
) -> None:
    """`documentId`가 어느 파일·제목인지 바로 찾아볼 수 있는 표. result.json이나
    verify-offsets 출력에서 documentId만 보고 어느 문서인지 궁금할 때 이걸 먼저 돌린다.

    **API 계약(§3)에는 없는, 사람이 볼 CLI 전용 출력**이다 — result.json 스키마는
    바뀌지 않는다.
    """
    workspace = json.loads((fixtures / "workspace.json").read_text(encoding="utf-8"))
    for doc in workspace["documents"]:
        typer.echo(f"{doc['documentId']:<6} {doc['department']:<5} {doc['title']:<20} {doc['file']}")


@app.command()
def score(
    result: Path = typer.Option(Path("out/result.json"), "--result"),
    gold: Path = typer.Option(Path("fixtures/gold.csv"), "--gold"),
) -> None:
    """result.json을 gold.csv로 채점한다."""
    response = ExtractResponse.model_validate_json(result.read_text(encoding="utf-8"))
    gold_rows = load_gold(gold)
    report = compute_score(response, gold_rows)
    typer.echo(format_report(report))


@app.command(name="verify-offsets")
def verify_offsets(
    result: Path = typer.Option(Path("out/result.json"), "--result"),
    fixtures: Path = typer.Option(Path("fixtures"), "--fixtures"),
) -> None:
    """result.json의 모든 occurrence가 실제 문서 내용과 글자 단위로 일치하는지 검증한다."""
    response = ExtractResponse.model_validate_json(result.read_text(encoding="utf-8"))
    request = load_fixture_request(fixtures)
    contents = {doc.documentId: doc.content for doc in request.documents}
    titles = {doc.documentId: doc.title for doc in request.documents}

    checks: list[tuple[str, object, bool, bool, bool]] = []
    for candidate in response.candidates:
        occurrence_lists = (
            [s.occurrences for s in candidate.senses]
            if isinstance(candidate, HomographCandidate)
            else [candidate.occurrences]
            if isinstance(candidate, GroupCandidate)
            else []
        )
        for occurrences in occurrence_lists:
            for occ in occurrences:
                content = contents.get(occ.documentId)
                span_ok = content is not None and content[occ.charStart:occ.charEnd] == occ.form
                snippet_ok = content is not None and make_snippet(content, occ.charStart, occ.charEnd) == occ.snippet
                checks.append((candidate.candidateId, occ, span_ok and snippet_ok, span_ok, snippet_ok))

    typer.echo(f"검사한 occurrence: {len(checks)}개\n")

    sample = random.sample(checks, k=min(5, len(checks))) if checks else []
    typer.echo("무작위로 고른 대조 결과:")
    for cid, occ, ok, _span_ok, _snippet_ok in sample:
        mark = "OK  " if ok else "FAIL"
        doc_label = f"{occ.documentId}({titles.get(occ.documentId, '?')})"
        typer.echo(f"  [{mark}] {cid} · {doc_label} · '{occ.form}' @ {occ.charStart}:{occ.charEnd}")
        typer.echo(f"          snippet: {occ.snippet}")

    failed = [c for c in checks if not c[2]]
    if failed:
        typer.echo(f"\n실패 {len(failed)}건:")
        for cid, occ, _ok, span_ok, snippet_ok in failed:
            reasons = []
            if not span_ok:
                reasons.append("charStart/charEnd가 form과 불일치")
            if not snippet_ok:
                reasons.append("snippet이 원문과 불일치")
            doc_label = f"{occ.documentId}({titles.get(occ.documentId, '?')})"
            typer.echo(f"  {cid} · {doc_label} · '{occ.form}' — {', '.join(reasons)}")
        raise typer.Exit(code=1)

    typer.echo("\n모든 occurrence 검증 통과.")


@app.command()
def archive(
    result: Path = typer.Option(Path("out/result.json"), "--result", help="스냅샷으로 남길 결과 파일"),
    label: str = typer.Option("", "--label", help="파일명에 붙일 라벨 (예: step2)"),
) -> None:
    """`out/history/`에 라벨을 붙여 한 번 더 남긴다.

    `extract`가 매번 자동으로 이력을 남기지만 라벨이 없다 — "이 시점이 3단계
    통과다"처럼 사람이 의미를 부여해 표시하고 싶을 때 이 명령을 따로 돌린다.
    """
    if not result.exists():
        typer.echo(f"{result} 가 없다 — 먼저 extract를 돌려라.")
        raise typer.Exit(code=1)

    dest = _archive_to_history(result, label=label)
    typer.echo(f"{result} 를 {dest} 로 남겼다.")


@app.command(name="usage-report")
def usage_report(
    log: Path = typer.Option(usage_log.DEFAULT_LOG_PATH, "--log", help="사용량 로그 파일 경로"),
    today: bool = typer.Option(False, "--today", help="오늘 기록만 집계"),
) -> None:
    """Gemini 호출 사용량 로그를 사람이 읽을 표로 집계한다.

    **지금(3단계 전)은 로그가 비어 있는 게 정상이다** — 4단계(SYNONYM)에서
    `pipeline/llm.py`가 매 호출마다 `usage_log.log_call()`을 부르기 시작하면
    그때부터 여기 쌓인다. 이 명령은 그 로그를 읽는 쪽만 지금 준비해 둔 것이다.
    """
    # 로그 timestamp는 UTC로 찍힌다(usage_log.LlmCallLog) — 로컬 date.today()와
    # 비교하면 시차만큼 날짜가 어긋난다(예: KST 새벽엔 UTC로 아직 어제). 반드시
    # UTC 기준 오늘로 비교한다.
    utc_today = datetime.now(timezone.utc).date()
    since = utc_today if today else None
    entries = usage_log.read_entries(log, since=since)

    if not entries:
        typer.echo(f"{log} — 기록된 호출이 없다.")
        typer.echo("4단계(SYNONYM)에서 pipeline/llm.py가 log_call()을 부르기 시작하면 여기 쌓인다.")
        return

    summary = usage_log.summarize(entries)
    typer.echo(f"기간: {'오늘' if today else '전체'}  ·  로그: {log}")
    typer.echo(f"총 호출 {summary.totalCalls}  (캐시 히트 {summary.cacheHits} · 실제 API 호출 {summary.realCalls})")
    typer.echo(f"입력 토큰 합계 {summary.totalInputTokens:,}  ·  출력 토큰 합계 {summary.totalOutputTokens:,}")
    if summary.byStage:
        typer.echo("stage별 호출 수:")
        for stage, count in summary.byStage.items():
            typer.echo(f"  {stage}: {count}")

    model = os.getenv("GEMINI_MODEL", "")
    limits = usage_log.limits_for_model(model) if model else None
    if limits:
        today_real_calls = sum(1 for e in usage_log.read_entries(log, since=utc_today) if not e.cacheHit)
        typer.echo(f"\n무료 티어 한도({model}): RPD {limits['rpd']}회 · RPM {limits['rpm']}회 · TPM {limits['tpm']:,}")
        typer.echo(f"오늘(UTC 기준) 실제 API 호출: {today_real_calls} / {limits['rpd']}  (캐시 히트는 한도를 소모하지 않음. 콘솔의 실제 리셋 시각은 태평양시 자정이라 약간 다를 수 있음)")
    elif model:
        typer.echo(f"\n'{model}'의 한도 정보가 없다 — SPEC.md §10 표에 없는 모델이니 콘솔에서 직접 확인.")

    typer.echo("")
    typer.echo(usage_log.format_timeline(entries))


if __name__ == "__main__":
    app()
