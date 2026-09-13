# 로컬 통합 테스트 스크립트

실제 백엔드·AWS 없이, `docker-compose.local.yml`(LocalStack + MySQL)로 전체
흐름(큐 → ubidict-py 소비·처리 → 큐)을 로컬에서 확인하기 위한 도구.

## 준비

```bash
cd ubidict-py
docker compose -f docker-compose.local.yml up -d      # LocalStack + MySQL 기동
python scripts/init_local_queues.py                     # 큐 2개 생성, 출력값을 .env에 붙여넣기
# .env에 GEMINI_API_KEY(_1..)도 채워야 한다 — 실제 판정에 Gemini를 쓰기 때문
uvicorn app.main:app --reload                            # ubidict-py 기동(컨슈머 자동 시작)
```

## 큐 흐름 테스트 (다른 터미널에서)

```bash
python scripts/stub_backend.py --type extract
python scripts/stub_backend.py --type contrast
```

`stub_backend.py`가 요청 큐에 메시지를 넣고, `ubidict-py`가 그걸 소비해
처리한 뒤 응답 큐로 결과를 발행하면, `stub_backend.py`가 그 응답을 받아
출력한다. 이게 되면 큐 기반 흐름 전체(멱등성 확인 → 처리 → 응답 발행 →
멱등성 기록 → 메시지 삭제)가 로컬에서 검증된 것이다.

## 이 스크립트들의 위치

`stub_backend.py`는 **진짜 백엔드가 아니다** — `app/queue_schema.py`의
초안 봉투 형식을 그대로 흉내내는 자리표시자다. 실제 백엔드 코드가 오면
(`reference/backend/` 참고) 이 스크립트는:
- 실제 계약과 봉투 형식이 다르면 같이 고치거나
- 백엔드 통합 전 회귀 테스트용으로 그대로 남겨두거나

둘 중 하나로 쓰면 된다.

## 정리

```bash
docker compose -f docker-compose.local.yml down -v   # 컨테이너+볼륨까지 삭제
```
