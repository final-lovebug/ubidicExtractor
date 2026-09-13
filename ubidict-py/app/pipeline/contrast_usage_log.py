"""§12 DictionaryContrast의 LLM 단계(`contrast_llm.py`) 전용 사용량 로그.

`usage_log.py`(extract 전용)와 파일도 스키마도 분리한다 — 토큰을 세는
방식(모델·inputTokens·outputTokens·cacheHit)은 같지만, 같이 남기는
"기준"(호출에서 뭘 찾았는지)이 다르다. extract는 `synonymGroupsFound`/
`homographsFound`가 의미 있지만, contrast는 그 대신 "LLM이 몇 건을
돌려줬고 그중 몇 건이 실제로 채택됐고 몇 건이 환각으로 버려졌는지"
(`matchesReturned`/`matchesAccepted`/`matchesDropped`)가 의미 있다.
공유 파일에 억지로 같이 적으면 어느 쪽이든 절반은 0/빈 배열로 찍혀서
읽어도 의미가 없다 — 그래서 파일 자체를 나눴다.

**단, 무료 티어 한도(RPD 등)는 여기서 다시 정의하지 않는다** — Google
쪽 쿼터는 extract·contrast 호출이 같은 모델이면 하나로 공유되는
사실이라, `usage_log.FREE_TIER_LIMITS`/`limits_for_model`을 그대로
가져다 쓴다. `app/cli.py`의 `usage-report`/`contrast-usage-report`
양쪽 다 "오늘 이 모델 실제 호출"을 집계할 때 **두 로그 파일을 모두
읽어 합산**해야 한다 — 한쪽만 보면 실제 소모한 쿼터보다 적게 보인다.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

DEFAULT_LOG_PATH = Path("logs/contrast_llm_usage.jsonl")
DEFAULT_DETAIL_LOG_PATH = Path("logs/contrast_llm_call_details.jsonl")


@dataclass
class ContrastCallLog:
    """호출 한 건. `usage_log.LlmCallLog`와 같은 목적이지만 필드가 다르다.

    `matchesReturned`/`matchesAccepted`/`matchesDropped`가 이 로그의
    핵심이다 — `matchesDropped`가 0이 아니면 프롬프트(`contrast_02_forbidden.md`)를
    더 다듬어야 한다는 신호다.
    """

    model: str
    inputTokens: int
    outputTokens: int
    elapsedMs: int
    cacheHit: bool
    jobId: str | None = None
    matchesReturned: int = 0
    matchesAccepted: int = 0
    matchesDropped: int = 0
    promptHash: str = ""
    note: str = ""
    keyIndex: int = 1  # §10 D-38 — 몇 번째 API 키로 성공했는지(키 값 자체는 절대 안 남긴다)
    modelIndex: int = 1  # §10 D-39 — `--model`에 콤마로 나열한 체인에서 몇 번째 모델로 성공했는지
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def log_call(entry: ContrastCallLog, log_path: Path = DEFAULT_LOG_PATH) -> None:
    """호출 한 건을 로그 파일에 한 줄로 append하고, 표준출력에도 같은 내용을 찍는다(CloudWatch용)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
    print(json.dumps({"logType": "contrast_llm_usage", **asdict(entry)}, ensure_ascii=False))


def read_entries(log_path: Path = DEFAULT_LOG_PATH, since: date | None = None) -> list[ContrastCallLog]:
    if not log_path.exists():
        return []
    entries: list[ContrastCallLog] = []
    with log_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = ContrastCallLog(**json.loads(line))
            if since is not None and datetime.fromisoformat(entry.timestamp).date() < since:
                continue
            entries.append(entry)
    return entries


@dataclass
class ContrastCallDetail:
    """`contrast_llm_usage.jsonl`과 `promptHash`로 연결되는 상세 기록.

    `droppedMatches`가 extract 쪽 상세 로그엔 없는, contrast만의 항목이다
    — 환각(존재하지 않는 termId·문서에 없는 matchedText)이 실제로 몇 건,
    어떤 이유로 버려졌는지 나중에도 다시 볼 수 있어야 튜닝이 된다.
    """

    promptHash: str
    inputDocuments: list[dict] = field(default_factory=list)
    outputMatches: list[dict] = field(default_factory=list)
    droppedMatches: list[dict] = field(default_factory=list)  # [{termId?, documentId?, matchedText?, reason}]
    # 사고 유도 스키마(§10 D-30)의 결과 — 모델이 판정 전에 실제로 검토했다고
    # 보고한 termId 목록. 채점에는 안 쓰고, "검토는 했는데 못 찾았는지 /
    # 아예 검토 목록에서 빠졌는지"를 구분해 튜닝할 때만 참고한다.
    consideredTermIds: list[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def log_call_detail(detail: ContrastCallDetail, path: Path = DEFAULT_DETAIL_LOG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(detail), ensure_ascii=False) + "\n")


def read_call_details(path: Path = DEFAULT_DETAIL_LOG_PATH) -> list[ContrastCallDetail]:
    if not path.exists():
        return []
    details: list[ContrastCallDetail] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            details.append(ContrastCallDetail(**json.loads(line)))
    return details


@dataclass
class ContrastUsageSummary:
    totalCalls: int
    cacheHits: int
    realCalls: int
    totalInputTokens: int
    totalOutputTokens: int
    totalMatchesAccepted: int
    totalMatchesDropped: int


def summarize(entries: list[ContrastCallLog]) -> ContrastUsageSummary:
    return ContrastUsageSummary(
        totalCalls=len(entries),
        cacheHits=sum(1 for e in entries if e.cacheHit),
        realCalls=sum(1 for e in entries if not e.cacheHit),
        totalInputTokens=sum(e.inputTokens for e in entries),
        totalOutputTokens=sum(e.outputTokens for e in entries),
        totalMatchesAccepted=sum(e.matchesAccepted for e in entries),
        totalMatchesDropped=sum(e.matchesDropped for e in entries),
    )


def format_timeline(entries: list[ContrastCallLog]) -> str:
    if not entries:
        return "(호출 이력 없음)"

    lines = ["호출 이력 (시간순):"]
    for i, e in enumerate(sorted(entries, key=lambda x: x.timestamp), start=1):
        ts = e.timestamp.replace("T", " ")[:16]
        kind = "캐시" if e.cacheHit else "실제호출"
        found = f"returned={e.matchesReturned} accepted={e.matchesAccepted} dropped={e.matchesDropped}"
        tokens = f"in={e.inputTokens} out={e.outputTokens}"
        prompt = f"prompt={e.promptHash}" if e.promptHash else ""
        key_note = f"key#{e.keyIndex}" if e.keyIndex != 1 else ""
        model_note = f"model#{e.modelIndex}({e.model})" if e.modelIndex != 1 else ""
        line = f"  {i}. {ts}  [{kind}]  {found}  {tokens}  {e.elapsedMs}ms  {prompt}  {key_note}  {model_note}".rstrip()
        if e.note:
            line += f"\n       note: {e.note}"
        lines.append(line)
    return "\n".join(lines)
