"""SPEC.md §6 — Gemini 호출. JSON Schema 강제·응답 캐시·호출 카운터·재시도.

이 모듈은 "판정"(어떤 forms가 SYNONYM/HOMOGRAPH인지)만 LLM에 맡긴다. 정확한
charStart/charEnd/snippet은 여기서 만들지 않는다 — `pipeline/synonym.py`가
LLM이 준 form(문자열)을 실제 문서에서 다시 찾아 계산한다(3단계 VARIANT와 같은
원칙: LLM은 의미 판단만, 오프셋은 결정론적 코드가 계산).

프롬프트는 §6이 정한 네 덩어리(`prompts/01_role.md`~`04_input.md`)를 읽어
이어붙인다. `03_existing.md`·`04_input.md`는 `{{EXISTING_TERMS}}`/`{{DOCUMENTS}}`
자리에 요청 내용을 런타임에 채워 넣는 템플릿이다.
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
from app.pipeline.model_chain import is_model_unavailable_failure, load_default_model_chain, parse_model_chain
from app.pipeline.usage_log import LlmCallDetail, LlmCallLog, log_call, log_call_detail
from app.pipeline.variant import find_frequent_terms
from app.schema import ExtractRequest, Usage

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


class LlmSynonymGroup(BaseModel):
    forms: list[str]
    proposedPreferredForm: str
    proposedEnglishName: str | None = None
    proposedDefinition: str
    confidence: float
    reason: str


class LlmHomographSense(BaseModel):
    label: str
    definition: str
    documentIds: list[str]  # 이 뜻으로 쓰인 문서들 — 오프셋은 여기서 다시 계산한다


class LlmHomographCandidate(BaseModel):
    form: str
    confidence: float
    reason: str
    senses: list[LlmHomographSense]


class LlmExtractionResult(BaseModel):
    # 방법 B — 판정 전에 후보를 먼저 훑도록 스키마로 강제한다(SPEC.md §10 D-9).
    # 최종 판정(homographs)과 별개로, "검토는 했다"는 걸 스키마에 남기게 해서
    # 사고 과정을 건너뛰지 못하게 하는 게 목적이다. 이 필드 자체는 채점에 안 쓴다.
    consideredHomographCandidates: list[str] = []
    synonymGroups: list[LlmSynonymGroup]
    homographs: list[LlmHomographCandidate]


class LlmCallLimitExceeded(RuntimeError):
    """§6 "호출 카운터" — 프로세스당 상한을 넘으면 예외를 던지고 중단한다."""


_call_count = 0  # 프로세스 안에서만 유효


def _build_prompt(request: ExtractRequest) -> str:
    role = (_PROMPTS_DIR / "01_role.md").read_text(encoding="utf-8")
    forbidden = (_PROMPTS_DIR / "02_forbidden.md").read_text(encoding="utf-8")
    existing_tpl = (_PROMPTS_DIR / "03_existing.md").read_text(encoding="utf-8")
    input_tpl = (_PROMPTS_DIR / "04_input.md").read_text(encoding="utf-8")

    existing_json = json.dumps(
        [t.model_dump() for t in request.existingTerms], ensure_ascii=False, indent=2
    )
    documents_text = "\n\n".join(
        f"### 문서 {d.documentId} ({d.department} · {d.title})\n{d.content}" for d in request.documents
    )
    # 방법 A' — 공백 기반(마크다운 무관) 고빈도 표기 힌트. SPEC.md §10 D-8·D-9 참고.
    frequent_terms = find_frequent_terms(request, min_documents=3)
    frequent_terms_text = (
        ", ".join(f"{term}({count}회·{docs}문서)" for term, count, docs in frequent_terms)
        if frequent_terms
        else "(해당 없음)"
    )

    existing_section = existing_tpl.replace("{{EXISTING_TERMS}}", existing_json)
    input_section = input_tpl.replace("{{DOCUMENTS}}", documents_text).replace(
        "{{FREQUENT_TERMS}}", frequent_terms_text
    )

    return "\n\n---\n\n".join([role, forbidden, existing_section, input_section])


def _cache_path(cache_dir: Path, prompt: str, model: str) -> Path:
    """§6 "sha256(프롬프트 + 입력)" — 모델도 출력에 영향을 주는 입력이므로 해시에
    같이 넣는다. 이게 없으면 같은 프롬프트로 모델만 바꿔 불렀을 때 이전 모델의
    캐시를 그대로 재사용해버린다(실제로 이 버그를 이번에 발견했다 — SPEC.md §10)."""
    digest = hashlib.sha256(f"{model}\n{prompt}".encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.json"


def _input_document_specs(request: ExtractRequest) -> list[dict]:
    """문서 원문이 아니라 명세(documentId·title·department)만 — 로그에 남길 몫."""
    return [{"documentId": d.documentId, "title": d.title, "department": d.department} for d in request.documents]


_PROMPT_ARCHIVE_DIR = Path("logs/prompts")
# 01/02만 스냅샷한다. 03_existing.md/04_input.md는 정적 템플릿이라({{PLACEHOLDER}}
# 그대로) 어느 호출이든 항상 내용이 똑같다 — 스냅샷해도 아무 정보가 안 된다.
# (실제로 그 호출에 어떤 문서·existingTerms가 들어갔는지는 `llm_call_details.jsonl`의
# `inputDocuments`가 이미 명세로 남긴다.)
_PROMPT_FILES = ["01_role.md", "02_forbidden.md"]


def _snapshot_prompt_files(prompt_hash: str) -> None:
    """`prompts/01_role.md`·`02_forbidden.md`를 고칠 때마다 `promptHash`가
    바뀐다 — 그 시점의 두 파일을 `logs/prompts/{hash}/`에 그대로 복사해 둔다.

    이러면 `llm_usage.jsonl`/`llm_call_details.jsonl`의 `promptHash`로 "그때
    01_role.md·02_forbidden.md가 정확히 어떤 내용이었는지"를 나중에도 다시 볼
    수 있다 — 튜닝 루프에서 무엇을 바꿔서 정밀도가 변했는지 재구성하는 데 쓴다.
    같은 해시 폴더가 이미 있으면 다시 쓰지 않는다(내용이 같다는 뜻이므로).

    **주의**: `promptHash`는 03/04가 채워진 최종 프롬프트 기준으로 계산되므로,
    문서·existingTerms만 바뀌어도 해시가 바뀔 수 있다 — 그런 경우 이 스냅샷의
    01/02 내용이 이전과 같아 보일 수 있는데, 그건 정상이다(01/02는 안 바뀐 것).
    """
    dest_dir = _PROMPT_ARCHIVE_DIR / prompt_hash
    if dest_dir.exists():
        return
    dest_dir.mkdir(parents=True, exist_ok=True)
    for filename in _PROMPT_FILES:
        # 바이트 그대로 복사한다 — 텍스트 모드로 읽고 쓰면 Windows에서 개행이
        # \n → \r\n으로 바뀌어, 내용은 같은데 원본과 바이트 단위로 달라진다.
        shutil.copy2(_PROMPTS_DIR / filename, dest_dir / filename)


def extract_synonyms_and_homographs(
    request: ExtractRequest,
    *,
    model: str | None = None,
    no_cache: bool = False,
    note: str = "",
) -> tuple[LlmExtractionResult, Usage]:
    """§5 ③ — 문서 전문을 한 컨텍스트에 넣고 1회 호출해 SYNONYM+HOMOGRAPH를 뽑는다.

    반환값의 `Usage`는 §3.2 응답의 `usage` 필드에 그대로 실린다 — 캐시 히트여도
    (참고용으로) 채워서 반환하지만, 캐시 히트는 `usage_log`에 `cacheHit=True`로
    남아 무료 티어 한도를 소모하지 않았음을 구분할 수 있다.

    `note`는 튜닝 루프에서 "이번에 뭘 바꿔서 다시 불렀는지"를 로그에 남기는
    한 줄 메모다 (`app.cli extract --note "..."`) — `usage-report`의 타임라인에
    그대로 찍힌다.
    """
    global _call_count

    # §10 D-39·D-40 — `--model`을 명시하면(콤마로 여러 개면 폴백 체인) 그걸
    # `.env`보다 우선한다. 명시 안 하면 `.env`의 GEMINI_MODEL_1/_2/...(또는
    # 단수 GEMINI_MODEL)를 자동으로 체인으로 쓴다. 콤마 없이 모델 하나만
    # 주면(지금까지 써온 `--model gemini-x-y`) 길이 1짜리 목록이라 "다음
    # 모델로" 분기가 실행될 일이 없다 — 재현성 테스트는 완전히 그대로 동작한다.
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
        response_schema=LlmExtractionResult,
    )

    last_model_error: Exception | None = None
    for model_index, m in enumerate(model_chain, start=1):
        cache_file = _cache_path(cache_dir, prompt, m)
        prompt_hash = cache_file.stem[:8]
        _snapshot_prompt_files(prompt_hash)

        if not no_cache and cache_file.exists():
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            result = LlmExtractionResult.model_validate(cached["result"])
            input_tokens = cached.get("inputTokens", 0)
            output_tokens = cached.get("outputTokens", 0)
            log_call(
                LlmCallLog(
                    stage="synonym_homograph",
                    model=m,
                    inputTokens=input_tokens,
                    outputTokens=output_tokens,
                    elapsedMs=0,
                    cacheHit=True,
                    jobId=request.jobId,
                    synonymGroupsFound=len(result.synonymGroups),
                    homographsFound=len(result.homographs),
                    promptHash=prompt_hash,
                    note=note,
                    modelIndex=model_index,
                )
            )
            log_call_detail(
                LlmCallDetail(
                    promptHash=prompt_hash,
                    inputDocuments=_input_document_specs(request),
                    outputSynonymGroups=[g.model_dump() for g in result.synonymGroups],
                    outputHomographs=[h.model_dump() for h in result.homographs],
                )
            )
            usage = Usage(model=m, inputTokens=input_tokens, outputTokens=output_tokens, llmCalls=0, elapsedMs=0)
            return result, usage

        if _call_count >= max_calls:
            raise LlmCallLimitExceeded(f"프로세스당 LLM 호출 상한({max_calls}회)을 넘었다 — 루프 사고를 의심하라.")

        # §10 D-38·D-40 — 키가 여러 개면 1번부터 시도하다가, 그 키 자체의
        # 문제(인증 실패·쿼터 소진)로 막히면 다음 키로 넘어간다. 5xx 과부하도
        # 마찬가지로 다음 키로 넘어간다(D-40) — 키가 서로 다른 프로젝트/쿼터에
        # 속할 수 있어 "5xx는 키를 바꿔도 소용없다"고 단정할 수 없다. 키 하나당
        # 5xx 재시도(§6, 최대 2회)는 그대로 유지 — "같은 키로 재시도"와 "다른
        # 키로 폴백"은 별개다.
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
                            response = client.models.generate_content(model=m, contents=prompt, config=config)
                            break
                        except errors.ServerError:
                            # 5xx·타임아웃만 2회까지, 지수 백오프. 4xx는 재시도하지 않는다(§6).
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
                    # 남은 키가 있는 한 마저 시도한다(모델을 바로 포기하지 않음).
                    if key_index < len(api_keys):
                        last_attempt_error = e
                        continue
                    raise
            else:
                assert last_attempt_error is not None
                raise last_attempt_error
        except (errors.ServerError, errors.ClientError) as e:
            # §10 D-39 — 여기 도달했다는 건 등록된 키를 전부 시도했다는 뜻(D-40)
            # — 그래도 이 모델이 지금 안 되면, 체인에 다음 모델이 남아있는 한
            # 그걸로 넘어간다.
            if is_model_unavailable_failure(e) and model_index < len(model_chain):
                last_model_error = e
                continue
            raise

        elapsed_ms = int((time.monotonic() - start) * 1000)
        result = LlmExtractionResult.model_validate_json(response.text)

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

        log_call(
            LlmCallLog(
                stage="synonym_homograph",
                model=m,
                inputTokens=input_tokens,
                outputTokens=output_tokens,
                elapsedMs=elapsed_ms,
                cacheHit=False,
                jobId=request.jobId,
                synonymGroupsFound=len(result.synonymGroups),
                homographsFound=len(result.homographs),
                promptHash=prompt_hash,
                note=note,
                keyIndex=used_key_index,
                modelIndex=model_index,
            )
        )
        log_call_detail(
            LlmCallDetail(
                promptHash=prompt_hash,
                inputDocuments=_input_document_specs(request),
                outputSynonymGroups=[g.model_dump() for g in result.synonymGroups],
                outputHomographs=[h.model_dump() for h in result.homographs],
            )
        )

        usage = Usage(model=m, inputTokens=input_tokens, outputTokens=output_tokens, llmCalls=1, elapsedMs=elapsed_ms)
        return result, usage
    else:
        assert last_model_error is not None
        raise last_model_error
