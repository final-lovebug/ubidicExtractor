"""app.service.run_extract_job/run_contrast_job 단위 테스트.

stub 모드는 DB·Gemini 둘 다 안 부르는 게 핵심이라, 그 두 지점(`fetch_*`,
`run_extract`/`run_contrast`)이 정말 호출 안 되는지를 모킹으로 확인한다.
real 모드는 DB 조회 결과가 파이프라인 함수에 그대로 전달되는지만 본다
(파이프라인 자체는 test/에서 이미 검증된 것 — 여기서 다시 안 본다).
`time.sleep`도 모킹해서 테스트가 실제로 2.5초씩 기다리지 않게 한다.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.job_schema import ContrastJobRequest, ExtractJobRequest
from app.schema import DictionaryEntry, DocumentInput, ExistingTerm
from app.service import run_contrast_job, run_extract_job

_DOC = DocumentInput(documentId="1", title="t", department="", content="본문")
_TERM = ExistingTerm(termId="1", preferredForm="구독자", englishName=None, synonyms=[])
_ENTRY = DictionaryEntry(termId="1", preferredForm="구독자", englishName=None, synonyms=[], definition="정의")


def _extract_job(mode: str = "real") -> ExtractJobRequest:
    return ExtractJobRequest(
        jobId="job-1", workspaceId=1, dictionaryVersionNo=1, dictionaryId=1,
        documentIds=[1, 2], accessToken="tok", mode=mode,
    )


def _contrast_job(mode: str = "real") -> ContrastJobRequest:
    return ContrastJobRequest(
        jobId="job-2", workspaceId=1, dictionaryVersionNo=1, dictionaryId=1,
        documentIds=[1], accessToken="tok", mode=mode,
    )


@patch("app.service.time.sleep")
@patch("app.service.fetch_existing_terms")
@patch("app.service.fetch_documents")
def test_extract_stub_mode_skips_db_and_returns_empty(mock_fetch_documents, mock_fetch_existing_terms, mock_sleep):
    with patch("app.service.run_extract") as mock_run_extract:
        response = run_extract_job(_extract_job(mode="stub"))

    mock_fetch_documents.assert_not_called()
    mock_fetch_existing_terms.assert_not_called()
    mock_run_extract.assert_not_called()
    mock_sleep.assert_called_once()

    assert response.status == "SUCCESS"
    assert response.candidates == []
    assert response.usage.model == "stub"
    assert response.usage.llmCalls == 0
    assert "stub" in response.warnings[0]


@patch("app.service.time.sleep")
@patch("app.service.fetch_dictionary_entries")
@patch("app.service.fetch_documents")
def test_contrast_stub_mode_skips_db_and_returns_empty(mock_fetch_documents, mock_fetch_dictionary_entries, mock_sleep):
    with patch("app.service.run_contrast") as mock_run_contrast:
        response = run_contrast_job(_contrast_job(mode="stub"))

    mock_fetch_documents.assert_not_called()
    mock_fetch_dictionary_entries.assert_not_called()
    mock_run_contrast.assert_not_called()
    mock_sleep.assert_called_once()

    assert response.suggestions == []
    assert response.usage.model == "stub"


@patch("app.service.fetch_existing_terms")
@patch("app.service.fetch_documents")
def test_extract_real_mode_fetches_from_db_and_delegates(mock_fetch_documents, mock_fetch_existing_terms):
    mock_fetch_documents.return_value = [_DOC]
    mock_fetch_existing_terms.return_value = [_TERM]

    fake_response = MagicMock()
    with patch("app.service.run_extract", return_value=fake_response) as mock_run_extract:
        result = run_extract_job(_extract_job(mode="real"))

    mock_fetch_documents.assert_called_once_with([1, 2])
    mock_fetch_existing_terms.assert_called_once_with(1)

    built_request = mock_run_extract.call_args.args[0]
    assert built_request.jobId == "job-1"
    assert built_request.workspaceId == "1"  # int -> str 변환됨(ExtractRequest 필드가 str)
    assert built_request.documents == [_DOC]
    assert built_request.existingTerms == [_TERM]
    assert result is fake_response


@patch("app.service.fetch_dictionary_entries")
@patch("app.service.fetch_documents")
def test_contrast_real_mode_fetches_from_db_and_delegates(mock_fetch_documents, mock_fetch_dictionary_entries):
    mock_fetch_documents.return_value = [_DOC]
    mock_fetch_dictionary_entries.return_value = [_ENTRY]

    fake_response = MagicMock()
    with patch("app.service.run_contrast", return_value=fake_response) as mock_run_contrast:
        result = run_contrast_job(_contrast_job(mode="real"))

    mock_fetch_documents.assert_called_once_with([1])
    mock_fetch_dictionary_entries.assert_called_once_with(1)

    built_request = mock_run_contrast.call_args.args[0]
    assert built_request.dictionary == [_ENTRY]
    assert built_request.documents == [_DOC]
    assert result is fake_response
