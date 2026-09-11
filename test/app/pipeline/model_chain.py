"""SPEC.md §6/§10 D-39·D-40 — Gemini 모델 폴백.

`llm.py`·`contrast_llm.py`가 공유한다. `api_keys.py`(D-38, 키 폴백)와 짝을
이루는 모듈이다 — 키 폴백은 "같은 모델, 다른 키"를, 이 모듈은 "그 모델의
키를 전부 소진했을 때 다음 모델로"를 담당한다.

모델 체인은 두 경로로 정해진다:
1. **`--model`에 콤마로 나열** — 그때그때 명시적으로 지정(D-39). 예:
   `--model "gemini-3.5-flash,gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite"`.
   콤마 없이 모델 하나만 주면(지금까지의 모든 테스트 방식) 폴백 없이 정확히
   그 모델만 쓴다 — 재현성 테스트 관행이 안 바뀐다. **`--model`을 주면 아래
   `.env` 체인은 완전히 무시된다** — "이번만 정확히 이 모델(들)로" 오버라이드다.
2. **`.env`에 `GEMINI_MODEL_1`, `_2`, ... 로 등록** — `--model`을 아예 안 주는
   기본 실행에서 자동으로 쓰인다(D-40). `api_keys.py`의 `GEMINI_API_KEY_1/_2`
   패턴과 똑같다 — 번호 붙은 게 하나도 없으면 기존 `GEMINI_MODEL`(단수) 하나로
   그대로 동작한다(마이그레이션 강제 없음).
"""

from __future__ import annotations

import os

from google.genai import errors

from app.pipeline.api_keys import is_key_specific_failure


def parse_model_chain(model_arg: str) -> list[str]:
    """콤마로 나열된 모델 문자열을 순서 있는 목록으로 바꾼다.

    콤마가 없으면 길이 1짜리 목록을 반환한다 — 이러면 호출부의 "다음 모델로"
    분기가 애초에 실행될 일이 없어, 기존 단일 모델 동작과 완전히 같다.
    """
    return [m.strip() for m in model_arg.split(",") if m.strip()]


def load_default_model_chain() -> list[str]:
    """`--model`을 안 줬을 때 쓸 체인을 `.env`에서 읽는다(§10 D-40).

    `GEMINI_MODEL_1`, `_2`, ... 를 순서대로 읽는다. 번호 붙은 게 하나도
    없으면 기존 `GEMINI_MODEL`(단수) 하나를 반환한다 — `api_keys.load_api_keys()`와
    같은 하위 호환 원칙. 둘 다 없으면 빈 목록(호출부가 에러를 던진다).
    """
    numbered: list[str] = []
    i = 1
    while True:
        m = os.getenv(f"GEMINI_MODEL_{i}")
        if not m:
            break
        numbered.append(m)
        i += 1

    if numbered:
        return numbered

    single = os.getenv("GEMINI_MODEL", "")
    return [single] if single else []


def is_model_unavailable_failure(exc: Exception) -> bool:
    """이 모델을 포기하고 다음 모델로 넘어가야 하는 실패인지.

    호출부(`llm.py`/`contrast_llm.py`의 키 루프)가 이 함수까지 예외를
    올려보낸다는 것 자체가 이미 "등록된 키를 전부 시도했다"는 뜻이다(§10
    D-40 — 5xx도 429/401/403과 동일하게 키를 전부 순회한 뒤에만 여기 도달
    하도록 고쳤다. 이전엔 5xx가 키 순회 없이 곧장 여기로 왔는데, 그건 "5xx는
    서버 과부하라 키를 바꿔도 소용없다"는, 키가 1개뿐이던 시절의 검증 안 된
    가정이었다 — 키가 여러 개면 서로 다른 프로젝트/쿼터일 수 있어 이 가정이
    틀릴 수 있다).

    `ServerError`(5xx)는 항상 True — 키를 전부 써도 과부하면 이 모델은
    지금 못 쓴다. `ClientError`는 `api_keys.is_key_specific_failure`로
    위임한다 — 참이면 인증/쿼터 문제로 키를 전부 소진한 것이고, 거짓이면
    진짜 프롬프트/스키마 문제라 모델을 바꿔도 똑같이 실패할 것이므로
    폴백하지 않는다(애초에 그런 경우는 키 루프에서 첫 키 만에 즉시 올라온다).
    """
    if isinstance(exc, errors.ServerError):
        return True
    if isinstance(exc, errors.ClientError):
        return is_key_specific_failure(exc)
    return False
