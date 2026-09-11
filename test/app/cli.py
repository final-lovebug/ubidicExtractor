"""SPEC.md §7 — HTTP보다 먼저 만드는 CLI.

    python -m app.cli extract --fixtures fixtures/ --out out/result.json
    python -m app.cli score   --result out/result.json --gold fixtures/gold.csv
    python -m app.cli verify-offsets --result out/result.json --fixtures fixtures/

`extract`는 HTTP를 거치지 않고 서비스 로직(`pipeline/`)을 직접 호출한다.

SPEC.md §12 — 사전집 대조(`DictionaryContrast`). `extract`와 완전히 분리된
파이프라인·픽스처·이력·사용량 로그다(§10 D-20):

    python -m app.cli contrast --fixtures fixtures/contrast --out out/contrast_result.json
    python -m app.cli score-contrast --result out/contrast_result.json --gold fixtures/contrast/gold.csv
    python -m app.cli archive-contrast --label baseline
    python -m app.cli contrast-usage-report
"""

from __future__ import annotations

import json
import os
import random
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import typer
from dotenv import load_dotenv

from app.pipeline import contrast_usage_log, usage_log
from app.pipeline.contrast import (
    compute_contrast_score,
    format_contrast_report,
    load_contrast_fixture_request,
    load_contrast_gold,
)
from app.pipeline.contrast_llm import find_llm_contrast_matches
from app.pipeline.llm import extract_synonyms_and_homographs
from app.pipeline.model_chain import load_default_model_chain
from app.pipeline.normalize import load_fixture_request, make_snippet
from app.pipeline.synonym import build_homograph_candidates, build_synonym_candidates
from app.pipeline.variant import find_variants
from app.schema import ContrastResponse, ExtractResponse, GroupCandidate, HomographCandidate, Usage
from app.scoring import compute_score, format_report, load_gold

load_dotenv()

app = typer.Typer(add_completion=False)


def _archive_to_history(result: Path, label: str = "", dest_dir: Path = Path("out/history")) -> Path:
    """`result`를 손대지 않고 `dest_dir`에 타임스탬프(+라벨) 붙여 복사한다.

    **덮어쓰지 않는다** — 부를 때마다 새 파일이 생긴다. `out/`는 `.gitignore`에
    있으므로 이 폴더들도 로컬에만 남고 커밋되지 않는다.

    `dest_dir` 기본값은 `out/history/`(extract 전용)다 — `contrast`는
    `out/contrast_history/`를 넘겨 완전히 분리된 이력을 쌓는다(§10 D-20).
    """
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    filename = f"{timestamp}__{label}.json" if label else f"{timestamp}.json"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    shutil.copy2(result, dest)
    return dest


_CONTRAST_HISTORY_DIR = Path("out/contrast_history")


def _today_real_calls_combined(model: str, utc_today: date) -> tuple[int, int]:
    """오늘 이 모델의 실제 API 호출을 extract·contrast 두 로그에서 합산한다.

    두 로그(`logs/llm_usage.jsonl`·`logs/contrast_llm_usage.jsonl`)는 파일이
    분리돼 있지만 같은 Gemini 모델을 부르면 **같은 일일 쿼터(RPD)를 나눠
    쓴다** — 한쪽만 보면 실제 소모량보다 적게 보인다(§10 D-20). 반환값은
    `(extract 실제 호출 수, contrast 실제 호출 수)`.
    """
    extract_calls = sum(
        1 for e in usage_log.read_entries(usage_log.DEFAULT_LOG_PATH, since=utc_today) if not e.cacheHit and e.model == model
    )
    contrast_calls = sum(
        1
        for e in contrast_usage_log.read_entries(contrast_usage_log.DEFAULT_LOG_PATH, since=utc_today)
        if not e.cacheHit and e.model == model
    )
    return extract_calls, contrast_calls


@app.command()
def extract(
    fixtures: Path = typer.Option(Path("fixtures"), "--fixtures", help="fixtures 폴더 경로"),
    out: Path = typer.Option(Path("out/result.json"), "--out", help="결과 JSON 저장 경로"),
    model: str = typer.Option(
        "", "--model", help="GEMINI_MODEL을 이번 실행만 덮어쓴다. 콤마로 여러 개 나열하면 모델 폴백 체인(§10 D-39)"
    ),
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

    `--model`에 콤마로 여러 모델을 나열하면 등록된 키(§10 D-38)를 전부 써도
    안 될 때 다음 모델로 자동 폴백한다(§10 D-39). 모델 하나만 주면 지금까지와
    똑같이 폴백 없이 그 모델만 쓴다.
    """
    request = load_fixture_request(fixtures)
    candidates: list[GroupCandidate | HomographCandidate] = list(find_variants(request))
    # --skip-llm 표시용 placeholder일 뿐 실제로 이 모델을 호출하지는 않는다 —
    # 폴백 체인의 첫 번째 값만 참고로 보여준다(§10 D-40).
    default_chain = load_default_model_chain()
    usage = Usage(
        model=model or (default_chain[0] if default_chain else "mock"),
        inputTokens=0,
        outputTokens=0,
        llmCalls=0,
        elapsedMs=0,
    )
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
def contrast(
    fixtures: Path = typer.Option(
        Path("fixtures/contrast"), "--fixtures", help="§12 전용 fixtures 폴더 (extract용 fixtures/와 분리)"
    ),
    out: Path = typer.Option(Path("out/contrast_result.json"), "--out", help="결과 JSON 저장 경로"),
    model: str = typer.Option(
        "", "--model", help="GEMINI_MODEL을 이번 실행만 덮어쓴다. 콤마로 여러 개 나열하면 모델 폴백 체인(§10 D-39)"
    ),
    no_cache: bool = typer.Option(False, "--no-cache", help="LLM 응답 캐시를 무시하고 다시 호출한다"),
    note: str = typer.Option("", "--note", help="튜닝 루프 메모 — usage-report 타임라인에 그대로 남는다"),
) -> None:
    """SPEC.md §12 사전집 대조. **LLM 단독(정의 기반)이다**(§10 D-22).
    실제 서비스 DB가 사전집에 동의어 목록을 저장하지 않기로 확정되면서,
    등재된 동의어를 리터럴로 스캔하던 규칙 단계는 제거했다. `--skip-llm`도
    함께 없앴다. LLM이 유일한 메커니즘이라 그 옵션을 쓰면 결과가 항상
    빈 배열이라 더 이상 의미가 없다.

    **매번 `out/contrast_history/`에도 자동으로 스냅샷을 남긴다.**
    `extract`가 `out/history/`에 남기는 것과 같은 이유이지만 폴더는
    완전히 분리했다(§10 D-20). 라벨을 붙이고 싶으면 `archive-contrast
    --label`을 따로 돌린다.

    `--model`에 콤마로 여러 모델을 나열하면 등록된 키(§10 D-38)를 전부 써도
    안 될 때 다음 모델로 자동 폴백한다(§10 D-39). 모델 하나만 주면 지금까지와
    똑같이 폴백 없이 그 모델만 쓴다.
    """
    request = load_contrast_fixture_request(fixtures)
    suggestions, usage = find_llm_contrast_matches(request, model=model or None, no_cache=no_cache, note=note)

    response = ContrastResponse(
        jobId=request.jobId,
        status="SUCCESS",
        dictionaryVersionNo=request.dictionaryVersionNo,
        suggestions=suggestions,
        usage=usage,
        warnings=[],
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(response.model_dump_json(indent=2), encoding="utf-8")
    history_path = _archive_to_history(out, dest_dir=_CONTRAST_HISTORY_DIR)
    typer.echo(f"{out} 에 저장했다. suggestions={len(suggestions)}개")
    typer.echo(f"LLM 호출: model={usage.model} inputTokens={usage.inputTokens} outputTokens={usage.outputTokens}")
    typer.echo(f"이력: {history_path}")


@app.command(name="score-contrast")
def score_contrast(
    result: Path = typer.Option(Path("out/contrast_result.json"), "--result"),
    gold: Path = typer.Option(Path("fixtures/contrast/gold.csv"), "--gold"),
) -> None:
    """contrast_result.json을 fixtures/contrast/gold.csv로 채점한다."""
    response = ContrastResponse.model_validate_json(result.read_text(encoding="utf-8"))
    gold_rows = load_contrast_gold(gold)
    report = compute_contrast_score(response.suggestions, gold_rows)
    typer.echo(format_contrast_report(report))


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


@app.command(name="archive-contrast")
def archive_contrast(
    result: Path = typer.Option(Path("out/contrast_result.json"), "--result", help="스냅샷으로 남길 결과 파일"),
    label: str = typer.Option("", "--label", help="파일명에 붙일 라벨 (예: rule-only)"),
) -> None:
    """`out/contrast_history/`에 라벨을 붙여 한 번 더 남긴다. `archive`의 contrast판(§10 D-20)."""
    if not result.exists():
        typer.echo(f"{result} 가 없다 — 먼저 contrast를 돌려라.")
        raise typer.Exit(code=1)

    dest = _archive_to_history(result, label=label, dest_dir=_CONTRAST_HISTORY_DIR)
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

    # §10 D-40 — `.env`에 폴백 체인(GEMINI_MODEL_1/_2/...)이 등록됐을 수 있다.
    # 여러 모델 전체의 한도를 한 번에 보여주는 건 범위 밖(D-39)이라, 1번째
    # (우선순위가 가장 높은) 모델 기준으로만 보여준다 — 나머지는 --model로
    # 직접 지정해서 이 명령을 다시 돌리거나 콘솔에서 확인.
    default_chain = load_default_model_chain()
    model = default_chain[0] if default_chain else ""
    chain_note = f"(폴백 체인 {len(default_chain)}개 중 1번째)" if len(default_chain) > 1 else ""
    limits = usage_log.limits_for_model(model) if model else None
    if limits:
        # 모델별로 RPD가 따로 관리된다(Google 쪽 쿼터가 모델 단위다) — 오늘 전체
        # 호출이 아니라 "지금 .env에 설정된 이 모델"의 호출만 세야 한다. 안 그러면
        # 다른 모델로 부른 것까지 이 모델의 한도를 갉아먹는 것처럼 보인다 —
        # gemini-3.5-flash처럼 RPD가 20으로 아주 낮은 모델에서는 이 차이가 크다.
        #
        # extract_calls는 이 로그(`--log` 인자, 기본 llm_usage.jsonl) 기준이고,
        # contrast_calls는 별도 로그(contrast_llm_usage.jsonl) 기준이다 — 같은
        # 모델이면 같은 RPD를 나눠 쓰므로 반드시 합산해서 보여준다(§10 D-20).
        extract_calls, contrast_calls = _today_real_calls_combined(model, utc_today)
        combined = extract_calls + contrast_calls
        typer.echo(f"\n무료 티어 한도({model}){chain_note}: RPD {limits['rpd']}회 · RPM {limits['rpm']}회 · TPM {limits['tpm']:,}")
        typer.echo(
            f"오늘(UTC 기준) 이 모델 실제 API 호출: {combined} / {limits['rpd']}"
            f"  (용어추출 {extract_calls} + 사전집 대조 {contrast_calls}, 같은 모델은 RPD를 공유한다."
            f" 캐시 히트는 한도를 소모하지 않음. 콘솔의 실제 리셋 시각은 태평양시 자정이라 약간 다를 수 있음)"
        )
    elif model:
        typer.echo(f"\n'{model}'의 한도 정보가 없다 — SPEC.md §10 표에 없는 모델이니 콘솔에서 직접 확인.")

    typer.echo("")
    typer.echo(usage_log.format_timeline(entries))


@app.command(name="contrast-usage-report")
def contrast_usage_report(
    log: Path = typer.Option(contrast_usage_log.DEFAULT_LOG_PATH, "--log", help="사전집 대조 LLM 사용량 로그 경로"),
    today: bool = typer.Option(False, "--today", help="오늘 기록만 집계"),
) -> None:
    """§12 LLM 단계 전용 사용량 로그를 집계한다. `usage-report`(extract 전용)의 contrast판(§10 D-20).

    `usage-report`와 파일도 스키마도 분리돼 있다. `matchesReturned`/
    `matchesAccepted`/`matchesDropped`가 extract 로그엔 없는 contrast만의
    지표다. 단, RPD 쿼터는 같은 모델이면 extract와 공유되므로 그 부분만
    두 로그를 합산해서 보여준다.
    """
    utc_today = datetime.now(timezone.utc).date()
    since = utc_today if today else None
    entries = contrast_usage_log.read_entries(log, since=since)

    if not entries:
        typer.echo(f"{log} — 기록된 호출이 없다.")
        typer.echo("contrast 명령을 --skip-llm 없이 돌리면 여기 쌓인다.")
        return

    summary = contrast_usage_log.summarize(entries)
    typer.echo(f"기간: {'오늘' if today else '전체'}  ·  로그: {log}")
    typer.echo(f"총 호출 {summary.totalCalls}  (캐시 히트 {summary.cacheHits} · 실제 API 호출 {summary.realCalls})")
    typer.echo(f"입력 토큰 합계 {summary.totalInputTokens:,}  ·  출력 토큰 합계 {summary.totalOutputTokens:,}")
    typer.echo(f"매치 채택 합계 {summary.totalMatchesAccepted}  ·  매치 환각(버려짐) 합계 {summary.totalMatchesDropped}")

    # §10 D-40 — usage-report와 같은 이유로 폴백 체인의 1번째 모델 기준만 보여준다.
    default_chain = load_default_model_chain()
    model = default_chain[0] if default_chain else ""
    chain_note = f"(폴백 체인 {len(default_chain)}개 중 1번째)" if len(default_chain) > 1 else ""
    limits = usage_log.limits_for_model(model) if model else None
    if limits:
        extract_calls, contrast_calls = _today_real_calls_combined(model, utc_today)
        combined = extract_calls + contrast_calls
        typer.echo(f"\n무료 티어 한도({model}){chain_note}: RPD {limits['rpd']}회 · RPM {limits['rpm']}회 · TPM {limits['tpm']:,}")
        typer.echo(
            f"오늘(UTC 기준) 이 모델 실제 API 호출: {combined} / {limits['rpd']}"
            f"  (사전집 대조 {contrast_calls} + 용어추출 {extract_calls}, 같은 모델은 RPD를 공유한다)"
        )
    elif model:
        typer.echo(f"\n'{model}'의 한도 정보가 없다 — SPEC.md §10 표에 없는 모델이니 콘솔에서 직접 확인.")

    typer.echo("")
    typer.echo(contrast_usage_log.format_timeline(entries))


if __name__ == "__main__":
    app()
