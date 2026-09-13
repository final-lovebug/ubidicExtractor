"""SPEC.md §3의 요청·응답 계약을 그대로 옮긴 Pydantic 모델.

이 파일은 계약(§3) 자체를 코드로 고정하는 것이 목적이다.
판정 로직(정규화·LLM 호출 등)은 여기 두지 않는다 — 그건 `pipeline/`의 몫이다.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


# ── 3.1 요청 ──────────────────────────────────────────────

class ExistingTerm(BaseModel):
    termId: str
    preferredForm: str
    englishName: str | None = None
    synonyms: list[str] = Field(default_factory=list)


class DocumentInput(BaseModel):
    documentId: str
    title: str
    department: str
    content: str


class ExtractRequest(BaseModel):
    jobId: str
    workspaceId: str
    dictionaryVersionNo: int
    existingTerms: list[ExistingTerm] = Field(default_factory=list)
    documents: list[DocumentInput] = Field(min_length=1)


# ── 3.3 / 3.4 후보 ────────────────────────────────────────

class Occurrence(BaseModel):
    documentId: str
    department: str
    form: str
    snippet: str
    charStart: int = Field(ge=0)
    charEnd: int = Field(ge=0)


class Sense(BaseModel):
    """HOMOGRAPH 전용 — 하나의 표기가 갈라지는 뜻 하나."""

    label: str
    definition: str
    occurrences: list[Occurrence] = Field(default_factory=list)


class GroupCandidate(BaseModel):
    """그룹형 후보 — 여러 표기가 하나의 개념을 가리키는 경우 (§3.3)."""

    candidateId: str
    kind: Literal["VARIANT", "SYNONYM"]
    forms: list[str]
    proposedPreferredForm: str
    proposedEnglishName: str | None = None
    proposedDefinition: str
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    occurrenceCount: int
    documentCount: int
    occurrences: list[Occurrence] = Field(default_factory=list)


class HomographCandidate(BaseModel):
    """갈래형 후보 — 하나의 표기가 여러 뜻으로 쓰이는 경우 (§3.4).

    `proposedPreferredForm`/`proposedDefinition`이 없다 — 사람이 갈래마다 정한다.
    """

    candidateId: str
    kind: Literal["HOMOGRAPH"]
    forms: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    occurrenceCount: int
    documentCount: int
    senses: list[Sense] = Field(min_length=2)


Candidate = Annotated[
    Union[GroupCandidate, HomographCandidate],
    Field(discriminator="kind"),
]


# ── 3.2 응답 ──────────────────────────────────────────────

class Usage(BaseModel):
    model: str
    inputTokens: int
    outputTokens: int
    llmCalls: int
    elapsedMs: int


class ExtractResponse(BaseModel):
    jobId: str
    status: Literal["SUCCESS", "PARTIAL", "FAILED"]
    dictionaryVersionNo: int
    candidates: list[Candidate] = Field(default_factory=list)
    usage: Usage
    warnings: list[str] = Field(default_factory=list)


# ── 3.6 그 외 엔드포인트 ──────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    model: str


# ── §12 DictionaryContrast — extract와 별개, 계약도 분리 ────
#
# `documents`는 §3.1의 `DocumentInput`을 그대로 재사용한다(§10 D-17).
# `dictionary`는 `ExistingTerm`을 상속만 하고 `definition`을 추가한
# `DictionaryEntry`를 쓴다(§10 D-19) — `ExistingTerm`(extract와 공유하는
# 타입) 자체는 건드리지 않는다. LLM 단계가 "이 용어가 무슨 개념인지"를
# 판단하려면 정의가 필요한데, `ExistingTerm`엔 그게 없었다.

class DictionaryEntry(ExistingTerm):
    definition: str


class ContrastRequest(BaseModel):
    jobId: str
    workspaceId: str
    dictionaryVersionNo: int
    dictionary: list[DictionaryEntry] = Field(min_length=1)
    documents: list[DocumentInput] = Field(min_length=1)


class ContrastSuggestion(BaseModel):
    termId: str
    preferredForm: str
    foundForm: str
    documentId: str
    department: str
    snippet: str
    charStart: int = Field(ge=0)
    charEnd: int = Field(ge=0)
    reason: str
    method: Literal["rule", "llm"] = "rule"


class ContrastResponse(BaseModel):
    jobId: str
    status: Literal["SUCCESS", "PARTIAL", "FAILED"]
    dictionaryVersionNo: int
    suggestions: list[ContrastSuggestion] = Field(default_factory=list)
    usage: Usage
    warnings: list[str] = Field(default_factory=list)
