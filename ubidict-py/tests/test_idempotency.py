"""app.idempotency 단위 테스트 — 실제 MySQL 없이 pymysql.connect를 모킹한다."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.idempotency import already_processed, mark_processed


def _mock_connection(fetchone_return=None):
    cursor = MagicMock()
    cursor.fetchone.return_value = fetchone_return
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn, cursor


@patch("app.idempotency.pymysql.connect")
def test_already_processed_true_when_row_exists(mock_connect):
    conn, cursor = _mock_connection(fetchone_return=(1,))
    mock_connect.return_value = conn

    assert already_processed("job-1") is True
    cursor.execute.assert_called_once_with("SELECT 1 FROM processed_jobs WHERE job_id = %s", ("job-1",))
    conn.close.assert_called_once()


@patch("app.idempotency.pymysql.connect")
def test_already_processed_false_when_no_row(mock_connect):
    conn, _cursor = _mock_connection(fetchone_return=None)
    mock_connect.return_value = conn

    assert already_processed("job-2") is False


@patch("app.idempotency.pymysql.connect")
def test_mark_processed_inserts_with_upsert(mock_connect):
    conn, cursor = _mock_connection()
    mock_connect.return_value = conn

    mark_processed("job-3", "extract")

    args, _kwargs = cursor.execute.call_args
    assert "INSERT INTO processed_jobs" in args[0]
    assert "ON DUPLICATE KEY UPDATE" in args[0]  # 경쟁 상태로 두 번 기록돼도 에러 안 남
    assert args[1] == ("job-3", "extract")
    conn.close.assert_called_once()


@patch("app.idempotency.pymysql.connect")
def test_connection_closed_even_on_query_error(mock_connect):
    """쿼리가 실패해도 연결은 반드시 닫는다(finally) — 커넥션 누수 방지."""
    conn, cursor = _mock_connection()
    cursor.execute.side_effect = RuntimeError("boom")
    mock_connect.return_value = conn

    try:
        already_processed("job-4")
    except RuntimeError:
        pass

    conn.close.assert_called_once()
