# 로컬 Spring ↔ SQS ↔ FastAPI 목 연동

`Spring dev` 프로필은 LocalStack의 `lovebug-llm-request` 큐에 `mode=MOCK` 요청을
발행한다. 이 워커는 모델·DB를 읽지 않고 고정 용어 추출 결과를 Spring의 HTTP
콜백으로 돌려준다. 완료 응답용 SQS 큐는 없다.

## 준비

먼저 Spring을 `dev` 프로필로 실행한다. Spring Boot Docker Compose가 LocalStack을
함께 띄우며, 첫 요청을 발행할 때 큐도 생성한다.

```bash
cd final/backend
./gradlew bootRun --args='--spring.profiles.active=dev'
```

그 다음 `ubidict-py/.env`에 아래 값을 둔다. LocalStack의 기본 계정 ID는
`000000000000`이다.

```dotenv
AWS_REGION=ap-northeast-2
SQS_ENDPOINT_URL=http://localhost:4566
SQS_REQUEST_QUEUE_URL=http://localhost:4566/000000000000/lovebug-llm-request
BACKEND_CALLBACK_BASE_URL=http://localhost:8080
AWS_ACCESS_KEY_ID=test
AWS_SECRET_ACCESS_KEY=test
```

워커를 호스트에서 실행한다.

```bash
cd final/ubidicExtractor/ubidict-py
uv run uvicorn app.main:app --reload
```

워커가 컨테이너에서 실행되는 경우 `BACKEND_CALLBACK_BASE_URL`은 보통
`http://host.docker.internal:8080`이어야 한다.

## 확인 방법

Spring에서 용어 추출 또는 문서 대조 작업을 접수한다. 작업 메시지의 `mode`가
`MOCK`이면 워커가 다음 콜백 중 하나를 호출한다.

- 용어 추출: `POST /api/internal/llm/extractions/{jobId}/result` — 고정 후보어 `결제` 1건
- 문서 대조: `POST /api/internal/llm/checks/{jobId}/result` — 빈 제안 목록

Spring은 `requestId`를 대조한 뒤 작업 상태를 완료로 바꾸며, 프론트는 기존 작업
조회 API를 폴링해 결과를 확인한다. 콜백이 5xx 또는 네트워크 오류면 워커는 SQS
메시지를 지우지 않아 재시도되고, 2xx·4xx면 메시지를 지운다.
