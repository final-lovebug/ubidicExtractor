-- 멱등성(idempotency) 전용 테이블 하나. app/idempotency.py 참고.
-- 다른 용도(작업 이력·사용량 로그)로 테이블을 늘리지 않는다 — CloudWatch가
-- 로그를 커버한다는 전제로 내린 결정(task.md 참고). 배포 시 이 파일을 한 번
-- 실행한다 — Alembic 같은 마이그레이션 프레임워크는 테이블 1개엔 과해서 안 쓴다.

CREATE TABLE IF NOT EXISTS processed_jobs (
    job_id VARCHAR(64) NOT NULL PRIMARY KEY,
    job_type VARCHAR(16) NOT NULL,
    processed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
