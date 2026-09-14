# 백엔드 연동 참고 자료

`final/backend`(Spring Boot) 리포를 직접 조사해서 알아낸 것과, 아직도 실제
코드가 없어서 확인이 필요한 것을 나눠 적는다. `app/queue_schema.py`의 메시지
봉투는 여전히 **초안**이다 — 백엔드가 실제로 `DI-5`/`DD-5`(추출·대조 요청
엔드포인트)를 구현하면 다시 맞춰야 한다.

## 이미 확인된 것 (2026-09-14, `final/backend` 실제 코드·마이그레이션 기준)

- **JWT**: HS256 공유 시크릿(`JwtProvider`, `Keys.hmacShaKeyFor`). JWKS 같은
  공개키 엔드포인트 없음 — `JWT_SECRET` 값을 그대로 공유해야 검증 가능하다.
  단, **FastAPI는 검증하지 않는다** — access token을 그대로 받아서(`accessToken`
  필드) 응답에 그대로 실어 돌려줄 뿐이다(`app/job_schema.py`).
- **문서 본문**: `document`(제목 등 메타) + `document_version.body`(TEXT, 실제
  본문, 최대 10,000자) — `document.current_version_no`가 최신 확정본을 가리킴.
  `app/backend_db.py`가 이 두 테이블을 직접 조인해서 읽는다.
- **사전집 용어**: `term(dictionary_id, preferred_form, english_name, definition)`
  — `synonyms` 컬럼 자체가 없음(test/SPEC.md D-22와 같은 결론).
  `department` 같은 필드는 이 스키마에 대응 컬럼이 없어 빈 문자열로 채운다.
- **DB**: 로컬은 `final/backend`의 `compose.yaml`이 띄우는 MySQL 컨테이너를
  그대로 공유해서 쓴다(별도 인스턴스 안 둠, `.env.example` 참고).
- **job 테이블**: `extraction_job`/`check_job`이 이미 마이그레이션돼 있다
  (`PENDING/RUNNING/SUCCEEDED/FAILED` 폴링 모델) — 그러나 **FastAPI는 이
  테이블에 안 쓴다**, 처리 결과는 지금처럼 SQS 응답 큐로만 돌려준다(사용자
  결정, 2026-09-14) — job 상태 갱신·draft_dictionary/draft_document 생성은
  백엔드가 응답을 받은 뒤 자기 책임으로 한다.
- **stub/real 모드**: 백엔드의 `app.ai.extractor.mode`/`app.ai.checker.mode`
  (`AI_EXTRACTOR_MODE`/`AI_CHECKER_MODE`, 기본 `stub`)와 같은 개념을
  `ExtractJobRequest`/`ContrastJobRequest.mode`로 그대로 가져왔다. 단, 백엔드
  쪽 `TermExtractorPort`/`TermCheckerPort`의 "real" 구현체는 아직 없다(스텁만
  있음) — 즉 백엔드가 이 서비스(FastAPI)를 그 포트의 실제 구현으로 쓸지, 아니면
  완전히 별도 경로(DI-5/DD-5 엔드포인트 → SQS → 이 서비스)로 갈지는 여전히
  백엔드 팀 설계에 달려있다.

## 여전히 확인 필요한 것

- [ ] SQS 요청 큐 이름/ARN, 응답 큐 이름/ARN — 실제로는 `lovebug-llm-request`/
      `-reply`/`-dlq`(계정 416121583617, ap-northeast-2)를 2026-09-13에 테스트로
      확인했다. **다만 이게 이 기능(용어 추출·사전집 대조) 전용으로 확정된
      큐인지는 백엔드 코드에 아직 안 나타난다** — 이름상 `llm`이라 그런 것
      같다는 추정일 뿐, 확답 필요.
- [ ] 요청 메시지의 실제 필드 구조 — `QueueTaskEnvelope.payload`가 이제
      `ExtractJobRequest`/`ContrastJobRequest`(jobId/workspaceId/
      dictionaryVersionNo/dictionaryId/documentIds/accessToken/mode) 형태라고
      가정했다. 실제로 백엔드가 이 필드명·타입 그대로 보내는지 확인 필요
      (Long인 ID를 JSON 숫자로 보낼지 문자열로 보낼지도 포함)
- [ ] `type`(`"extract"`|`"contrast"`) 구분을 메시지 바디에 넣을지, 큐 자체를
      종류별로 나눌지
- [ ] 실패 시 정책 — `queue_consumer.py`는 실패해도 매번 FAILED 응답을 즉시
      발행한다(재시도마다 중복 발행 가능). 백엔드가 "가장 최근 상태만 신뢰"
      하는 방식인지 확인 필요
- [ ] `accessToken`을 왜 돌려받는지 — 백엔드가 이걸로 뭘 하는지(응답 발신자
      검증? 로깅?) 확인하면 이 서비스 쪽 처리(그냥 에코)가 맞는지 재확인 가능

## 여기에 받아 넣을 것 (예시)

- 백엔드 쪽 큐 프로듀서/컨슈머 코드 스니펫(Spring Cloud AWS 등) — `DI-5`/`DD-5` 구현 시
- 실제 메시지 스키마 정의(JSON Schema·DTO 클래스 등)
- 인프라(Terraform/CDK) 중 SQS·IAM 관련 부분만 발췌
