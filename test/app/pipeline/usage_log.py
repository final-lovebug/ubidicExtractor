"""Gemini 호출 사용량을 기록·집계하는 인프라.

**지금은 이 모듈을 호출하는 곳이 없다.** 실제 Gemini 호출 코드(`pipeline/llm.py`)는
4단계(SYNONYM)에서 만든다 — 그때 매 호출(캐시 히트 포함)마다 `log_call()`을
부르게 될 자리를 미리 마련해 두는 것이 이 파일의 목적이다.

로그는 JSON Lines(한 줄 = 한 호출)로 append한다. 프로세스가 중간에 죽어도
이전 줄은 그대로 유효하고, 사람이 에디터나 `type`/`cat`으로 바로 열어볼 수 있다.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

DEFAULT_LOG_PATH = Path("logs/llm_usage.jsonl")
DEFAULT_DETAIL_LOG_PATH = Path("logs/llm_call_details.jsonl")

# SPEC.md §10 D-2 표 그대로 — 모델이 바뀌면 여기만 고친다.
# 2.0/1.5 계열은 2026-09 기준 이미 종료됐거나 종료 예정이다(§10 D-2 갱신 참고).
# 정확한 값은 콘솔의 https://ai.google.dev/gemini-api/docs/rate-limits 에서 다시 확인할 것 —
# 여기 값은 웹 검색 기준이라 프로젝트별 쿼터와 다를 수 있다.
FREE_TIER_LIMITS: dict[str, dict[str, int]] = {
    # 3.5~3.8 "flash"(Lite 아님)는 사용자가 콘솔에서 직접 확인한 값 — RPD 20은
    # flash-lite 계열(1,500)의 1/75 수준이니 튜닝 루프를 아주 아껴 써야 한다.
    "gemini-3.5-flash": {"rpm": 5, "tpm": 250_000, "rpd": 20},  # §10 D-14, 콘솔에서 직접 확인
    "gemini-3.6-flash": {"rpm": 5, "tpm": 250_000, "rpd": 20},
    "gemini-3.7-flash": {"rpm": 5, "tpm": 250_000, "rpd": 20},
    "gemini-3.8-flash": {"rpm": 5, "tpm": 250_000, "rpd": 20},
    "gemini-3.5-flash-lite": {"rpm": 30, "tpm": 250_000, "rpd": 1_500},  # 2026-09-09부터 사용 중 (§10 D-11)
    "gemini-3.1-flash-lite": {"rpm": 30, "tpm": 1_000_000, "rpd": 1_500},
    "gemini-2.0-flash": {"rpm": 15, "tpm": 1_000_000, "rpd": 1_500},
    "gemini-2.0-flash-lite": {"rpm": 30, "tpm": 1_000_000, "rpd": 1_500},  # 2026-06-01 종료됨
    "gemini-1.5-pro": {"rpm": 2, "tpm": 32_000, "rpd": 50},
    "gemini-1.5-flash": {"rpm": 15, "tpm": 1_000_000, "rpd": 1_500},
    "gemini-1.5-flash-8b": {"rpm": 15, "tpm": 1_000_000, "rpd": 1_500},
}


def _normalize_model_key(model: str) -> str:
    return model.strip().lower().replace(" ", "-")


def limits_for_model(model: str) -> dict[str, int] | None:
    return FREE_TIER_LIMITS.get(_normalize_model_key(model))


@dataclass
class LlmCallLog:
    """호출 한 건. `cacheHit=True`면 API를 실제로 부르지 않은 것이므로
    토큰 수는 캐시된 응답 기준(참고용)이고 무료 티어 한도는 소모하지 않는다.

    `synonymGroupsFound`/`homographsFound`/`promptHash`/`note`는 튜닝 루프
    진행 상황을 로그만 보고 알 수 있게 하려고 추가했다 — 토큰 수만으로는
    "이번 호출에서 뭘 찾았는지, 뭘 바꿔서 다시 불렀는지"가 안 보였다.
    옛날 로그 줄에는 이 필드들이 없을 수 있으므로 전부 기본값을 둔다
    (하위 호환 — `LlmCallLog(**json.loads(old_line))`이 그대로 동작한다).
    """

    stage: str  # 예: "synonym_homograph"
    model: str
    inputTokens: int
    outputTokens: int
    elapsedMs: int
    cacheHit: bool
    jobId: str | None = None
    synonymGroupsFound: int = 0
    homographsFound: int = 0
    promptHash: str = ""  # 프롬프트 sha256 앞 8자리 — .cache/{hash}.json과 대응
    note: str = ""  # "이번에 뭘 바꿔서 다시 불렀는지" 한 줄 메모 (--note로 입력)
    keyIndex: int = 1  # §10 D-38 — 몇 번째 API 키로 성공했는지(키 값 자체는 절대 안 남긴다)
    modelIndex: int = 1  # §10 D-39 — `--model`에 콤마로 나열한 체인에서 몇 번째 모델로 성공했는지
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def log_call(entry: LlmCallLog, log_path: Path = DEFAULT_LOG_PATH) -> None:
    """호출 한 건을 로그 파일에 한 줄로 append한다."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")


def read_entries(log_path: Path = DEFAULT_LOG_PATH, since: date | None = None) -> list[LlmCallLog]:
    """로그 파일을 읽어 파싱한다. `since`를 주면 그 날짜 이후 기록만 반환한다."""
    if not log_path.exists():
        return []
    entries: list[LlmCallLog] = []
    with log_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = LlmCallLog(**json.loads(line))
            if since is not None and datetime.fromisoformat(entry.timestamp).date() < since:
                continue
            entries.append(entry)
    return entries


@dataclass
class LlmCallDetail:
    """`llm_usage.jsonl`(요약)과 `promptHash`로 연결되는 상세 기록.

    `llm_usage.jsonl`은 손대지 않는다(사용자 지정) — 대신 "정확히 어떤 문서가
    입력으로 들어갔는지"·"Gemini가 실제로 뭘 리턴했는지"는 이 별도 파일에
    남긴다. 문서 원문은 넣지 않는다 — `inputDocuments`는 명세(documentId·
    title·department)일 뿐이다.
    """

    promptHash: str
    inputDocuments: list[dict] = field(default_factory=list)
    outputSynonymGroups: list[dict] = field(default_factory=list)
    outputHomographs: list[dict] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def log_call_detail(detail: LlmCallDetail, path: Path = DEFAULT_DETAIL_LOG_PATH) -> None:
    """상세 기록 한 건을 append한다 — `llm_usage.jsonl`과 1:1로, `promptHash`로 연결된다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(detail), ensure_ascii=False) + "\n")


def read_call_details(path: Path = DEFAULT_DETAIL_LOG_PATH) -> list[LlmCallDetail]:
    if not path.exists():
        return []
    details: list[LlmCallDetail] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            details.append(LlmCallDetail(**json.loads(line)))
    return details


@dataclass
class UsageSummary:
    totalCalls: int
    cacheHits: int
    realCalls: int
    totalInputTokens: int
    totalOutputTokens: int
    byStage: dict[str, int]


def summarize(entries: list[LlmCallLog]) -> UsageSummary:
    by_stage: dict[str, int] = {}
    for entry in entries:
        by_stage[entry.stage] = by_stage.get(entry.stage, 0) + 1
    return UsageSummary(
        totalCalls=len(entries),
        cacheHits=sum(1 for e in entries if e.cacheHit),
        realCalls=sum(1 for e in entries if not e.cacheHit),
        totalInputTokens=sum(e.inputTokens for e in entries),
        totalOutputTokens=sum(e.outputTokens for e in entries),
        byStage=by_stage,
    )


def format_timeline(entries: list[LlmCallLog]) -> str:
    """호출을 시간순으로 한 줄씩 — 튜닝 루프가 어떻게 진행됐는지 한눈에 보려는 것.

    토큰 수만으로는 "이번 호출에서 뭘 찾았는지"를 알 수 없어서 만들었다.
    """
    if not entries:
        return "(호출 이력 없음)"

    lines = ["호출 이력 (시간순):"]
    for i, e in enumerate(sorted(entries, key=lambda x: x.timestamp), start=1):
        # "2026-09-09T03:34:12.345+00:00" → "2026-09-09 03:34"
        ts = e.timestamp.replace("T", " ")[:16]
        kind = "캐시" if e.cacheHit else "실제호출"
        found = f"synonym={e.synonymGroupsFound} homograph={e.homographsFound}"
        tokens = f"in={e.inputTokens} out={e.outputTokens}"
        prompt = f"prompt={e.promptHash}" if e.promptHash else ""
        key_note = f"key#{e.keyIndex}" if e.keyIndex != 1 else ""
        model_note = f"model#{e.modelIndex}({e.model})" if e.modelIndex != 1 else ""
        line = f"  {i}. {ts}  [{kind}]  {found}  {tokens}  {e.elapsedMs}ms  {prompt}  {key_note}  {model_note}".rstrip()
        if e.note:
            line += f"\n       note: {e.note}"
        lines.append(line)
    return "\n".join(lines)
