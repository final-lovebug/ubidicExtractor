"""SPEC.md §12 — 정의 기반 맥락 매칭 (LLM 단독). §10 D-22 참고.

**이제 이게 §12의 유일한 매칭 로직이다.** 예전엔 "등재된 동의어 문자열이
문서에 그대로 나타나는지" 보는 규칙 단계가 따로 있었는데, 실제 서비스
DB가 사전집 항목에 `synonyms`를 저장하지 않기로 확정되면서 제거했다
(그 입력이 실제로는 항상 빈 배열이라 규칙 단계가 영원히 아무것도 못
찾았을 것이다 — §10 D-22). 그래서 지금은 리터럴로 그대로 쓰인 경우든,
`definition`만 보고 맥락으로 풀어쓴 경우든 **전부 LLM 한 번의 호출로
찾는다.**

`pipeline/llm.py`(extract의 SYNONYM/HOMOGRAPH 호출)와 똑같은 인프라를
그대로 쓴다 — 캐시(sha256(model+prompt)), 호출 카운터, 재시도(5xx만
2회). 새로 만드는 건 프롬프트·스키마·후처리뿐이다.

**단, 사용량 로그는 `usage_log`(extract 전용)를 쓰지 않고 `contrast_usage_log`로
분리했다(§10 D-20)** — 토큰을 세는 방식은 같지만, 같이 남기는 "기준"(무엇을
찾았는지)이 서로 달라서다. extract는 synonymGroups/homographs 개수가
의미 있고, contrast는 "LLM이 몇 건을 돌려줬고 몇 건이 채택/환각으로
버려졌는지"가 의미 있다. **단, 두 로그가 같은 Gemini 모델의 같은 일일
쿼터(RPD)를 나눠 쓰므로, 쿼터 확인(`usage-report`/`contrast-usage-report`)은
반드시 두 로그를 합산해서 봐야 한다.**

**LLM은 의미 판단만, 오프셋은 결정론적 코드가 계산한다**(§5 ③ 원칙,
`synonym.py`와 동일) — LLM이 돌려준 `matchedText`를 `find_occurrences`로
문서에서 다시 찾아 실제 offset을 계산한다. 그 과정에서 세 가지를 검증하고,
실패하면 그 매치를 버리고 경고를 남긴다:
1. `termId`가 `request.dictionary`에 실재하는가
2. `matchedText`가 해당 문서에 리터럴로 실제 존재하는가
3. `matchedText`가 `preferredForm`과 정확히 같지는 않은가 — 같으면 이미
   올바른 표기라 제안할 게 없다(예전 규칙 단계의 `if form == preferredForm:
   continue`가 하던 일 중 코드로 보장 가능한 부분만 되살린 것)
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from app.pipeline.api_keys import is_key_specific_failure, load_api_keys
from app.pipeline.contrast_usage_log import ContrastCallDetail, ContrastCallLog, log_call, log_call_detail
from app.pipeline.model_chain import is_model_unavailable_failure, load_default_model_chain, parse_model_chain
from app.pipeline.normalize import find_occurrences, make_snippet
from app.schema import ContrastRequest, ContrastSuggestion, Usage

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
_PROMPT_FILES = ["contrast_01_role.md", "contrast_02_forbidden.md"]
_PROMPT_ARCHIVE_DIR = Path("logs/prompts")


class LlmContrastMatch(BaseModel):
    termId: str
    documentId: str
    matchedText: str
    reason: str
    confidence: float


class LlmContrastResult(BaseModel):
    # 사고 유도 스키마 — 판정 전에 사전집 termId 전부를 검토했다는 걸
    # 스키마로 강제한다. HOMOGRAPH 단계의 consideredHomographCandidates와
    # 같은 기법(§10 D-9), §12엔 D-30에서 처음 적용. matches보다 먼저
    # 선언해야 한다 — 모델이 스키마 필드 순서대로 채워나가므로, 정답
    # 필드보다 앞에 둬야 "먼저 다 훑어보고 나서 판정한다"는 순서가 강제된다.
    # 이 필드 자체는 채점에 안 쓴다.
    consideredTermIds: list[str] = []
    matches: list[LlmContrastMatch]


class LlmCallLimitExceeded(RuntimeError):
    """§6 "호출 카운터" — llm.py와 같은 프로세스 전역 카운터를 공유한다."""


_call_count = 0


def _build_prompt(request: ContrastRequest) -> str:
    role = (_PROMPTS_DIR / "contrast_01_role.md").read_text(encoding="utf-8")
    forbidden = (_PROMPTS_DIR / "contrast_02_forbidden.md").read_text(encoding="utf-8")
    dictionary_tpl = (_PROMPTS_DIR / "contrast_03_dictionary.md").read_text(encoding="utf-8")
    input_tpl = (_PROMPTS_DIR / "contrast_04_input.md").read_text(encoding="utf-8")

    # synonyms는 뺀다 — 실제 DB엔 그 필드가 없어 항상 빈 배열이다(§10 D-22).
    # 늘 빈 배열인 필드를 프롬프트에 넣는 건 토큰 낭비이자 노이즈다.
    dictionary_json = json.dumps(
        [
            {"termId": t.termId, "preferredForm": t.preferredForm, "englishName": t.englishName, "definition": t.definition}
            for t in request.dictionary
        ],
        ensure_ascii=False,
        indent=2,
    )
    documents_text = "\n\n".join(
        f"### 문서 {d.documentId} ({d.department} · {d.title})\n{d.content}" for d in request.documents
    )

    dictionary_section = dictionary_tpl.replace("{{DICTIONARY}}", dictionary_json)
    forbidden_section = forbidden.replace("{{DICTIONARY}}", dictionary_json)
    input_section = input_tpl.replace("{{DOCUMENTS}}", documents_text)

    return "\n\n---\n\n".join([role, forbidden_section, dictionary_section, input_section])


def _cache_path(cache_dir: Path, prompt: str, model: str) -> Path:
    digest = hashlib.sha256(f"{model}\n{prompt}".encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.json"


def _input_document_specs(request: ContrastRequest) -> list[dict]:
    return [{"documentId": d.documentId, "title": d.title, "department": d.department} for d in request.documents]


def _snapshot_prompt_files(prompt_hash: str) -> None:
    dest_dir = _PROMPT_ARCHIVE_DIR / prompt_hash
    if dest_dir.exists():
        return
    dest_dir.mkdir(parents=True, exist_ok=True)
    for filename in _PROMPT_FILES:
        shutil.copy2(_PROMPTS_DIR / filename, dest_dir / filename)


def _to_suggestions(
    request: ContrastRequest, result: LlmContrastResult
) -> tuple[list[ContrastSuggestion], list[dict]]:
    """LLM이 돌려준 matches를 실제 offset이 붙은 ContrastSuggestion으로 바꾼다.

    환각(존재하지 않는 termId·문서에 없는 matchedText)은 조용히 버리지 않고
    경고를 찍은 뒤 버린다 — AGENTS.md "숫자를 꾸미지 마라" 원칙대로, 몇 건이
    왜 버려졌는지 콘솔에도 즉시 보이고, `contrast_llm_call_details.jsonl`에도
    `droppedMatches`로 구조화해 남긴다(§10 D-20) — extract 쪽 로그엔 없는,
    contrast만의 "기준"이다.

    반환값의 두 번째 요소가 버려진 매치 목록(`{termId, documentId, matchedText, reason}`)이다.
    """
    dictionary_by_id = {t.termId: t for t in request.dictionary}
    documents_by_id = {d.documentId: d for d in request.documents}

    suggestions: list[ContrastSuggestion] = []
    dropped: list[dict] = []
    for match in result.matches:
        term = dictionary_by_id.get(match.termId)
        if term is None:
            print(f"[contrast_llm] 경고: 사전집에 없는 termId '{match.termId}' — 버림")
            dropped.append({"termId": match.termId, "documentId": match.documentId, "reason": "termId가 사전집에 없음"})
            continue
        doc = documents_by_id.get(match.documentId)
        if doc is None:
            print(f"[contrast_llm] 경고: 존재하지 않는 documentId '{match.documentId}' — 버림")
            dropped.append({"termId": match.termId, "documentId": match.documentId, "reason": "documentId가 요청에 없음"})
            continue
        if match.matchedText.strip() == term.preferredForm:
            print(f"[contrast_llm] 안내: '{match.matchedText}'는 이미 선호 표기 그대로라 제안할 게 없음 — 버림")
            dropped.append(
                {
                    "termId": match.termId,
                    "documentId": match.documentId,
                    "matchedText": match.matchedText,
                    "reason": "이미 선호 표기 그대로 쓰임(제안 불필요)",
                }
            )
            continue
        spans = find_occurrences(doc.content, match.matchedText)
        if not spans:
            print(f"[contrast_llm] 경고: matchedText '{match.matchedText}'가 {doc.documentId}에 실재하지 않음 — 버림")
            dropped.append(
                {
                    "termId": match.termId,
                    "documentId": match.documentId,
                    "matchedText": match.matchedText,
                    "reason": "matchedText가 문서 원문에 없음(환각 의심)",
                }
            )
            continue
        start, end = spans[0]
        suggestions.append(
            ContrastSuggestion(
                termId=term.termId,
                preferredForm=term.preferredForm,
                foundForm=match.matchedText,
                documentId=doc.documentId,
                department=doc.department,
                snippet=make_snippet(doc.content, start, end),
                charStart=start,
                charEnd=end,
                reason=match.reason,
                method="llm",
            )
        )
    return suggestions, dropped


def find_llm_contrast_matches(
    request: ContrastRequest,
    *,
    model: str | None = None,
    no_cache: bool = False,
    note: str = "",
) -> tuple[list[ContrastSuggestion], Usage]:
    """§12 2단계 — 사전집 정의 기반으로 문서를 훑어 맥락상 일치를 찾는다.

    `llm.py`의 `extract_synonyms_and_homographs`와 같은 구조 — 캐시·카운터·
    재시도·로깅을 전부 재사용한다.
    """
    global _call_count

    # §10 D-39·D-40 — llm.py와 동일. `--model` 명시(콤마면 체인)가 `.env`보다
    # 우선하고, 안 주면 `.env`의 GEMINI_MODEL_1/_2/...(또는 단수 GEMINI_MODEL)를
    # 자동으로 체인으로 쓴다. 콤마 없는 단일 모델은 지금까지와 완전히 동일.
    model_chain = parse_model_chain(model) if model else load_default_model_chain()
    if not model_chain:
        raise RuntimeError("GEMINI_MODEL이 설정되지 않았다 — .env를 확인하라.")

    cache_dir = Path(os.getenv("LLM_CACHE_DIR", ".cache"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    max_calls = int(os.getenv("MAX_LLM_CALLS", "20"))
    temperature = float(os.getenv("GEMINI_TEMPERATURE", "0.1"))

    prompt = _build_prompt(request)  # 모델과 무관한 내용이라 한 번만 만든다

    config = types.GenerateContentConfig(
        temperature=temperature,
        response_mime_type="application/json",
        response_schema=LlmContrastResult,
    )

    last_model_error: Exception | None = None
    for model_index, current_model in enumerate(model_chain, start=1):
        cache_file = _cache_path(cache_dir, prompt, current_model)
        prompt_hash = cache_file.stem[:8]
        _snapshot_prompt_files(prompt_hash)

        if not no_cache and cache_file.exists():
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            result = LlmContrastResult.model_validate(cached["result"])
            input_tokens = cached.get("inputTokens", 0)
            output_tokens = cached.get("outputTokens", 0)
            suggestions, dropped = _to_suggestions(request, result)
            log_call(
                ContrastCallLog(
                    model=current_model,
                    inputTokens=input_tokens,
                    outputTokens=output_tokens,
                    elapsedMs=0,
                    cacheHit=True,
                    jobId=request.jobId,
                    matchesReturned=len(result.matches),
                    matchesAccepted=len(suggestions),
                    matchesDropped=len(dropped),
                    promptHash=prompt_hash,
                    note=note,
                    modelIndex=model_index,
                )
            )
            log_call_detail(
                ContrastCallDetail(
                    promptHash=prompt_hash,
                    inputDocuments=_input_document_specs(request),
                    outputMatches=[m.model_dump() for m in result.matches],
                    droppedMatches=dropped,
                    consideredTermIds=result.consideredTermIds,
                )
            )
            usage = Usage(
                model=current_model, inputTokens=input_tokens, outputTokens=output_tokens, llmCalls=0, elapsedMs=0
            )
            return suggestions, usage

        if _call_count >= max_calls:
            raise LlmCallLimitExceeded(f"프로세스당 LLM 호출 상한({max_calls}회)을 넘었다 — 루프 사고를 의심하라.")

        # §10 D-38·D-40 — llm.py와 동일한 키 폴백. 키가 여러 개면 1번부터
        # 시도하다가, 인증 실패·쿼터 소진이면 다음 키로 넘어간다. 5xx 과부하도
        # 마찬가지로 다음 키로 넘어간다(D-40) — 키가 서로 다른 프로젝트/쿼터일
        # 수 있어 "5xx는 키를 바꿔도 소용없다"고 단정할 수 없다.
        api_keys = load_api_keys()
        last_attempt_error: Exception | None = None
        used_key_index = 1
        try:
            for key_index, api_key in enumerate(api_keys, start=1):
                client = genai.Client(api_key=api_key)
                start = time.monotonic()
                attempt = 0
                try:
                    while True:
                        try:
                            _call_count += 1
                            response = client.models.generate_content(model=current_model, contents=prompt, config=config)
                            break
                        except errors.ServerError:
                            attempt += 1
                            if attempt > 2:
                                raise
                            time.sleep(2**attempt)
                    used_key_index = key_index
                    break
                except errors.ClientError as e:
                    if is_key_specific_failure(e) and key_index < len(api_keys):
                        last_attempt_error = e
                        continue
                    raise
                except errors.ServerError as e:
                    # §10 D-40 — 이 키의 2회 재시도가 다 소진된 뒤에도 5xx면,
                    # 남은 키가 있는 한 마저 시도한다.
                    if key_index < len(api_keys):
                        last_attempt_error = e
                        continue
                    raise
            else:
                assert last_attempt_error is not None
                raise last_attempt_error
        except (errors.ServerError, errors.ClientError) as e:
            # §10 D-39 — 여기 도달했다는 건 등록된 키를 전부 시도했다는
            # 뜻(D-40) — 그래도 이 모델이 지금 안 되면 다음 모델로 넘어간다.
            if is_model_unavailable_failure(e) and model_index < len(model_chain):
                last_model_error = e
                continue
            raise

        elapsed_ms = int((time.monotonic() - start) * 1000)
        result = LlmContrastResult.model_validate_json(response.text)

        usage_metadata = response.usage_metadata
        input_tokens = getattr(usage_metadata, "prompt_token_count", 0) or 0
        output_tokens = getattr(usage_metadata, "candidates_token_count", 0) or 0

        cache_file.write_text(
            json.dumps(
                {"result": result.model_dump(), "inputTokens": input_tokens, "outputTokens": output_tokens},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        suggestions, dropped = _to_suggestions(request, result)

        log_call(
            ContrastCallLog(
                model=current_model,
                inputTokens=input_tokens,
                outputTokens=output_tokens,
                elapsedMs=elapsed_ms,
                cacheHit=False,
                jobId=request.jobId,
                matchesReturned=len(result.matches),
                matchesAccepted=len(suggestions),
                matchesDropped=len(dropped),
                promptHash=prompt_hash,
                note=note,
                keyIndex=used_key_index,
                modelIndex=model_index,
            )
        )
        log_call_detail(
            ContrastCallDetail(
                promptHash=prompt_hash,
                inputDocuments=_input_document_specs(request),
                outputMatches=[m.model_dump() for m in result.matches],
                droppedMatches=dropped,
                consideredTermIds=result.consideredTermIds,
            )
        )

        usage = Usage(
            model=current_model, inputTokens=input_tokens, outputTokens=output_tokens, llmCalls=1, elapsedMs=elapsed_ms
        )
        return suggestions, usage
    else:
        assert last_model_error is not None
        raise last_model_error
