"""백엔드(`final/backend`) DB에서 문서 본문·사전집 용어를 직접 읽는다.

**읽기 전용** — 여기서 백엔드 소유 테이블에 쓰지 않는다(§10 D-38 이후 계속
지켜온 "남의 경계는 침범하지 않는다" 원칙과 같다. 이번엔 반대로 "남의 데이터를
읽는" 쪽이라 더더욱 조심스럽다). 처리 결과는 지금처럼 SQS 응답 큐로만 돌려준다
— `extraction_job`/`check_job` 같은 백엔드 소유 테이블의 상태는 여기서 갱신하지
않는다(그건 백엔드가 응답을 받은 뒤 자기 책임으로 한다).

테이블 스키마는 `final/backend/src/main/resources/db/migration/`의 Flyway
마이그레이션에서 그대로 가져왔다(추측 아님, 2026-09-14 확인):

- `document(id, workspace_id, title, current_version_no, ...)` — 본문 없음.
  현재 확정본은 `current_version_no`가 가리키는 `document_version` 행이다
  (V200__create_document_and_version.sql).
- `document_version(id, document_id, version_no, body, ...)` — 본문(body)은
  여기 유일하게 있다. TEXT(최대 10,000자).
- `term(id, dictionary_id, preferred_form, english_name, definition, ...)` —
  `synonyms` 컬럼 자체가 없다(test/SPEC.md D-22와 같은 결론 — 실 DB는 동의어를
  저장하지 않는다).

`department`는 이 스키마에 대응하는 컬럼이 없다(`document`/`workspace` 둘 다
없음) — §3.5 원래 취지는 "부서마다 다르게 부른다"를 보여주는 표시값이었는데,
실제 도메인 모델엔 그 개념이 없다. 빈 문자열로 채운다 — 판정 로직은 이 값을
안 쓰고(occurrence 표시용일 뿐) 억지로 값을 만들어내지 않는다.
"""

from __future__ import annotations

import os

import pymysql
import pymysql.cursors

from app.schema import DictionaryEntry, DocumentInput, ExistingTerm


def _connect() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DATABASE"),
        charset="utf8mb4",  # 명시 안 하면 pymysql이 latin1로 붙어 한글이 깨진다(실측으로 발견)
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
    )


def fetch_documents(document_ids: list[int]) -> list[DocumentInput]:
    """`documentIds`로 현재 확정본 본문을 한 번에 조회한다.

    소프트 삭제(`deleted_at`)된 문서는 제외한다. 존재하지 않거나 삭제된
    documentId는 결과에서 조용히 빠진다 — 호출부(`service.py`)가 요청한
    개수와 실제로 돌아온 개수를 비교해 경고할 수 있다.
    """
    if not document_ids:
        return []
    placeholders = ",".join(["%s"] * len(document_ids))
    sql = f"""
        SELECT d.id AS document_id, d.title AS title, dv.body AS body
        FROM document d
        JOIN document_version dv
          ON dv.document_id = d.id AND dv.version_no = d.current_version_no
        WHERE d.id IN ({placeholders}) AND d.deleted_at IS NULL
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, document_ids)
            rows = cur.fetchall()
    finally:
        conn.close()

    return [
        DocumentInput(
            documentId=str(row["document_id"]),
            title=row["title"],
            department="",  # 스키마에 대응 컬럼 없음 — 위 모듈 docstring 참고
            content=row["body"],
        )
        for row in rows
    ]


def fetch_existing_terms(dictionary_id: int) -> list[ExistingTerm]:
    """extract용 — `definition` 없이 termId/preferredForm/englishName만."""
    return [
        ExistingTerm(termId=str(t.termId), preferredForm=t.preferredForm, englishName=t.englishName, synonyms=[])
        for t in _fetch_terms(dictionary_id)
    ]


def fetch_dictionary_entries(dictionary_id: int) -> list[DictionaryEntry]:
    """contrast용 — `definition` 포함."""
    return _fetch_terms(dictionary_id)


def _fetch_terms(dictionary_id: int) -> list[DictionaryEntry]:
    sql = """
        SELECT id AS term_id, preferred_form, english_name, definition
        FROM term
        WHERE dictionary_id = %s
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (dictionary_id,))
            rows = cur.fetchall()
    finally:
        conn.close()

    return [
        DictionaryEntry(
            termId=str(row["term_id"]),
            preferredForm=row["preferred_form"],
            englishName=row["english_name"],
            synonyms=[],  # term 테이블에 synonyms 컬럼 자체가 없다
            definition=row["definition"],
        )
        for row in rows
    ]
