"""app.backend_db 단위 테스트 — 실제 MySQL 없이 pymysql.connect를 모킹한다.

여기 쓰는 SQL은 `final/backend`의 Flyway 마이그레이션(V200/V300)에서 그대로
가져온 실제 테이블·컬럼명 기준이다(추측 아님, 2026-09-14 확인) — 이 테스트는
그 매핑(DB 컬럼 → Pydantic 필드)이 맞는지만 본다, 쿼리가 실제로 그 스키마에
대해 도는지는 로컬 backend-mysql-1로 별도 확인 필요.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.backend_db import fetch_dictionary_entries, fetch_documents, fetch_existing_terms


def _mock_connection(fetchall_return):
    cursor = MagicMock()
    cursor.fetchall.return_value = fetchall_return
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn, cursor


def test_fetch_documents_empty_ids_skips_query_entirely():
    with patch("app.backend_db.pymysql.connect") as mock_connect:
        result = fetch_documents([])
    assert result == []
    mock_connect.assert_not_called()


@patch("app.backend_db.pymysql.connect")
def test_fetch_documents_maps_columns_to_document_input(mock_connect):
    rows = [{"document_id": 1, "title": "결제 API 명세", "body": "본문 내용"}]
    conn, cursor = _mock_connection(rows)
    mock_connect.return_value = conn

    result = fetch_documents([1, 2])

    args, _ = cursor.execute.call_args
    assert "FROM document d" in args[0]
    assert "JOIN document_version dv" in args[0]
    assert "d.deleted_at IS NULL" in args[0]
    assert args[1] == [1, 2]

    assert len(result) == 1
    assert result[0].documentId == "1"
    assert result[0].title == "결제 API 명세"
    assert result[0].department == ""  # 실 스키마에 대응 컬럼 없음
    assert result[0].content == "본문 내용"
    conn.close.assert_called_once()


@patch("app.backend_db.pymysql.connect")
def test_fetch_existing_terms_has_no_definition(mock_connect):
    rows = [{"term_id": 10, "preferred_form": "구독자", "english_name": "Subscriber", "definition": "정의"}]
    conn, cursor = _mock_connection(rows)
    mock_connect.return_value = conn

    result = fetch_existing_terms(dictionary_id=5)

    args, _ = cursor.execute.call_args
    assert "FROM term" in args[0]
    assert args[1] == (5,)

    assert len(result) == 1
    assert result[0].termId == "10"
    assert result[0].preferredForm == "구독자"
    assert result[0].englishName == "Subscriber"
    assert result[0].synonyms == []  # term 테이블에 컬럼 자체가 없음
    assert not hasattr(result[0], "definition")  # ExistingTerm엔 definition이 없다


@patch("app.backend_db.pymysql.connect")
def test_fetch_dictionary_entries_includes_definition(mock_connect):
    rows = [{"term_id": 10, "preferred_form": "구독자", "english_name": None, "definition": "정의"}]
    conn, cursor = _mock_connection(rows)
    mock_connect.return_value = conn

    result = fetch_dictionary_entries(dictionary_id=5)

    assert len(result) == 1
    assert result[0].termId == "10"
    assert result[0].englishName is None
    assert result[0].definition == "정의"
