"""SPEC.md §6/§10 D-38 — Gemini API 키 순차 폴백.

`llm.py`·`contrast_llm.py`가 공유한다(§10 D-20이 `usage_log`를 공유한 것과
같은 원칙 — 인프라는 중복 구현하지 않고 재사용한다).

**하는 일**: 키가 여러 개(`GEMINI_API_KEY_1`, `_2`, ...) 등록돼 있으면 1번부터
순서대로 쓰다가, 지금 쓰는 키 자체의 문제(인증 실패·쿼터 소진)로 막히면 다음
키로 넘어간다. 기존처럼 `GEMINI_API_KEY`(단수)만 있어도 그대로 "키 1개"로
동작한다 — 마이그레이션을 강제하지 않는다.

**안 하는 일 (D-38 참고)**: 키별 RPD 잔량 추적, 라운드로빈. 지금 측정된
필요("막히면 다음 키로")를 넘어서는 복잡도는 넣지 않는다.
"""

from __future__ import annotations

import os

from google.genai import errors

# 이 코드가 "이 키를 포기하고 다음 키로" 폴백할 대상으로 보는 상태 코드.
# 401(인증 실패)·403(권한 없음)·429(쿼터 소진, RESOURCE_EXHAUSTED) — 셋 다
# "이 키 자체의 문제"다. 그 외 4xx(예: 400 — 프롬프트/스키마 문제)는 키를
# 바꿔도 똑같이 실패하므로 폴백 대상이 아니다 — §6 "4xx는 재시도하지 않는다"는
# 원칙은 그대로 유지된다. 여기서 하는 건 "같은 요청 재시도"가 아니라 "다른
# 키로 넘어가기"라 별개의 동작이다.
_KEY_SPECIFIC_CODES = (401, 403, 429)

# 실제로 무효한 키를 넣고 검증해보니(§10 D-38), 무효 키는 401/403이 아니라
# **400 INVALID_ARGUMENT**로 오고, code만으로는 "진짜 프롬프트/스키마 문제로
# 인한 400"과 구분이 안 된다. 대신 응답 JSON 안쪽에 `reason: API_KEY_INVALID`가
# 실려 온다 — 이 reason으로만 구분해서, code 기반 판정을 건드리지 않고 이
# 케이스만 추가로 잡는다.
_KEY_SPECIFIC_REASONS = {"API_KEY_INVALID", "PERMISSION_DENIED", "UNAUTHENTICATED"}


def _extract_reason(exc: errors.ClientError) -> str | None:
    try:
        for detail in exc.details.get("error", {}).get("details", []):
            if "reason" in detail:
                return detail["reason"]
    except (AttributeError, TypeError):
        pass
    return None


def load_api_keys() -> list[str]:
    """`GEMINI_API_KEY_1`, `_2`, ... 를 순서대로 읽는다.

    번호 붙은 키가 하나도 없으면 `GEMINI_API_KEY`(단수) 하나를 반환한다 —
    기존 `.env`를 그대로 쓰는 사용자는 동작이 안 바뀐다.
    """
    numbered: list[str] = []
    i = 1
    while True:
        key = os.getenv(f"GEMINI_API_KEY_{i}")
        if not key:
            break
        numbered.append(key)
        i += 1

    if numbered:
        return numbered

    single = os.getenv("GEMINI_API_KEY")
    if not single:
        raise RuntimeError("GEMINI_API_KEY(_1)이 .env에 없다.")
    return [single]


def is_key_specific_failure(exc: errors.ClientError) -> bool:
    """이 키를 포기하고 다음 키로 넘어가야 하는 실패인지."""
    if exc.code in _KEY_SPECIFIC_CODES:
        return True
    return _extract_reason(exc) in _KEY_SPECIFIC_REASONS
