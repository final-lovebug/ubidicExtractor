"""SQS 표준 큐의 "최소 한 번 전달"로 인한 중복 처리를 막는 멱등성 체크.

MySQL 테이블 딱 하나(`db/schema.sql`의 `processed_jobs`)만 쓴다. 다른 용도
(작업 이력·사용량 로그)로 테이블을 늘리지 않는다 — 그건 CloudWatch가
커버한다(task.md 참고).

호출 순서(`queue_consumer.py`)는 반드시 **처리 → 응답 큐 발행 → `mark_processed`
→ SQS 메시지 삭제**다. 먼저 기록부터 하면, 기록 직후 프로세스가 죽었을 때
"처리 안 했는데 처리한 걸로" 영구히 남아 그 작업이 다시는 처리되지 않는다
(백엔드가 응답을 영영 못 받음) — 반대로 순서를 지키면 최악의 경우도 "응답이
중복 발행될 수 있다" 정도라 훨씬 안전하다(task.md에 알려진 트레이드오프로
기록).

연결을 호출마다 새로 여는 가장 단순한 방식이다 — 테이블 하나, 호출량도
많지 않아 커넥션 풀은 지금 필요 없다(측정된 필요가 생기면 그때 추가).
"""

from __future__ import annotations

import os

import pymysql


def _connect() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DATABASE"),
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=5,
    )


def already_processed(job_id: str) -> bool:
    """이 jobId를 이미 끝까지 처리했는지(응답 큐 발행까지 끝났는지)."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM processed_jobs WHERE job_id = %s", (job_id,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def mark_processed(job_id: str, job_type: str) -> None:
    """처리·응답 발행이 끝난 jobId를 기록한다. 이미 있으면 조용히 넘어간다
    (경쟁 상태로 동시에 두 번 기록되는 것까지 막아준다 — 유니크 키 위반을
    에러로 올리지 않는다)."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO processed_jobs (job_id, job_type) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE job_type = job_type",
                (job_id, job_type),
            )
    finally:
        conn.close()
