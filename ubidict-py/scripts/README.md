# 로컬 통합 테스트 스크립트

실제 백엔드·AWS 없이, `docker-compose.local.yml`(LocalStack) + `final/backend`의
MySQL로 전체 흐름(큐 → ubidict-py 소비·처리(DB 조회 포함) → 큐)을 로컬에서
확인하기 위한 도구.

## 준비

```bash
# 1) final/backend 쪽 MySQL을 띄운다(이 리포가 아니라 backend 리포에서) —
#    문서/사전집을 이 DB에서 직접 읽으므로 별도 MySQL을 안 둔다(2026-09-14).
cd ../final/backend && docker compose up -d mysql
docker port backend-mysql-1                              # 호스트 포트 확인(재시작마다 바뀔 수 있음)
# → ubidict-py/.env의 MYSQL_PORT에 그 값을 넣는다

# 2) 이 프로젝트의 멱등성 테이블을 그 DB에 한 번 만든다(최초 1회만)
docker exec -i backend-mysql-1 mysql -uubidict -pubidict ubidict < ubidict-py/db/schema.sql

cd ubidict-py
docker compose -f docker-compose.local.yml up -d          # LocalStack(SQS만) 기동
python scripts/init_local_queues.py                        # 큐 2개 생성, 출력값을 .env에 붙여넣기
# .env에 GEMINI_API_KEY(_1..)도 채워야 한다 — real 모드에서 Gemini를 쓰기 때문
uvicorn app.main:app --reload                               # ubidict-py 기동(컨슈머 자동 시작)
```

## 큐 흐름 테스트 (다른 터미널에서)

```bash
python scripts/stub_backend.py --type extract                          # mode=stub 기본값 — DB/Gemini 안 씀
python scripts/stub_backend.py --type extract --mode real \
    --document-ids 1 2 --dictionary-id 1                                # 실제 DB에 있는 ID여야 함
```

`stub_backend.py`가 요청 큐에 메시지를 넣고, `ubidict-py`가 그걸 소비해
처리한 뒤(`mode=real`이면 DB에서 문서·사전집을 읽고 Gemini까지 부른다)
응답 큐로 결과를 발행하면, `stub_backend.py`가 그 응답을 받아 출력한다.
이게 되면 큐 기반 흐름 전체(멱등성 확인 → DB 조회 → 처리 → 응답 발행 →
멱등성 기록 → 메시지 삭제)가 로컬에서 검증된 것이다 — 2026-09-14에 실제
AWS SQS + 실제 backend MySQL로 이미 이 순서 그대로 확인했다(task.md 참고).

## 이 스크립트들의 위치

`stub_backend.py`는 **진짜 백엔드가 아니다** — `app/job_schema.py`/
`app/queue_schema.py`의 초안 형식을 그대로 흉내내는 자리표시자다. 실제
백엔드 코드가 오면(`reference/backend/` 참고) 이 스크립트는:
- 실제 계약과 형식이 다르면 같이 고치거나
- 백엔드 통합 전 회귀 테스트용으로 그대로 남겨두거나

둘 중 하나로 쓰면 된다.

## 정리

```bash
docker compose -f docker-compose.local.yml down -v   # LocalStack 컨테이너+볼륨 삭제
# backend-mysql-1은 final/backend 쪽 리소스라 여기서 안 건드린다
```
