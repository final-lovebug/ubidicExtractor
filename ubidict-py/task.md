# ubidict-py 작업 추적

## 배경

`test/`는 CLI로 검증한 R&D 프로토타입(SPEC.md §10 D-1~D-40). 여기(`ubidict-py/`)는
그 로직을 실제 서비스로 옮긴 것 — SQS로 요청을 받고 결과를 SQS로 돌려주며,
멱등성 확인용 MySQL을 하나 쓴다.

**`test/`의 기존 문서(AGENTS.md·SPEC.md §2/§11)와 다른 점**: 거기엔 "Kafka는
Spring이 담당, 이 서비스는 HTTP만"·"DB 붙이지 마라"고 돼 있는데, 이건 R&D
당시 판단이고 지금은 뒤집혔다 — 이 서비스가 직접 SQS를 구독하고 MySQL도
쓴다. `test/` 문서는 그대로 두고(과거 기록 보존) 새 아키텍처는 여기 적는다.

## 확정된 결정

| 항목 | 결정 |
|---|---|
| 메시지큐 | SQS |
| 결과 회신 | 응답용 큐(reply queue)로 발행 |
| DB | **2026-09-14부터 `final/backend`의 MySQL을 그대로 공유** — 멱등성 테이블(`processed_jobs`) 하나만 우리 것이고, `document`/`document_version`/`term`은 읽기 전용으로 직접 조회. 별도 DB 인스턴스 안 둠 |
| 문서·사전집 전달 방식 | **2026-09-14부터 ID 기반** — 요청은 `documentIds`/`dictionaryId`만 받고, 본문·용어는 `app/backend_db.py`가 DB에서 직접 읽는다(인라인 전송 안 함) |
| 인증 | JWT(`accessToken`)는 **검증하지 않는다** — 그대로 받아서 응답에 그대로 에코 |
| stub/real 모드 | 요청(`ExtractJobRequest`/`ContrastJobRequest.mode`)에 포함. `stub`(기본값)이면 DB·Gemini 둘 다 안 부르고 2.5초 뒤 빈 결과, `real`이면 실제 처리. 백엔드의 `app.ai.extractor.mode`/`checker.mode`와 같은 개념 |
| 배포 | ECR + CodeDeploy(ECS 블루/그린으로 추정). 실제 ECS/IAM/RDS 프로비저닝(Terraform/CDK)은 범위 밖 |
| CI/CD | **GitHub Actions로 확정**(`.github/workflows/`) |
| 모니터링 | CloudWatch(표준출력 구조화 JSON 로그 → awslogs 드라이버). OpenTelemetry는 미정 — 나중에 추가할 자리만 |

## 작업 단계

- [x] 1. 프로젝트 뼈대 — `pyproject.toml`·`.env.example`·`.gitignore` + `test/app/`에서 포팅
      (`schema.py`, `pipeline/{api_keys,model_chain,variant,synonym,llm,contrast_llm,usage_log,contrast_usage_log}.py`,
      `pipeline/normalize.py`는 fixture 로더 빼고 포팅, `prompts/*.md` 8개 복사)
- [x] 2. `app/service.py` — `run_extract`/`run_contrast` 단일 진입점(HTTP·큐 공용)
- [x] 3. `app/main.py` — `/health`·`/extract`·`/contrast`를 mock에서 `service.py` 호출로 교체
- [x] 4. `db/schema.sql` + `app/idempotency.py` — 멱등성 체크·기록
- [x] 5. `app/queue_schema.py`(초안) + `app/queue_consumer.py` — SQS 롱폴링, 멱등성 확인 → 처리 → 응답 큐 발행 → 기록 → 삭제
- [x] 6. `usage_log.py`/`contrast_usage_log.py`에 표준출력 구조화 JSON 로그 추가(로컬 JSONL은 유지)
- [x] 7. `reference/backend/README.md` 자리 마련 — 백엔드 코드 도착 시 `queue_schema.py` 재검토 필요
- [x] 8. `Dockerfile` + `deploy/{appspec.yaml,taskdef.json,buildspec.yml}` 작성
- [x] 9. 검증:
  - `docker build` 성공 확인
  - 로컬 `uvicorn`/컨테이너 기동 + `curl /health` 확인
  - **`test/fixtures`로 실제 `/extract`·`/contrast` 스모크 테스트 완료**: `/extract`는 실제 Gemini 호출로 15개 후보 반환(gemini-3.5-flash, D-40 자동 모델 체인 정상 작동), `/contrast`는 23개 제안 반환(`구독자→이용자` 등 test/의 D-23 골드셋과 일치하는 결과 확인) — 포팅이 로직을 안 깨뜨렸음을 확인
  - **실제 AWS SQS(`lovebug-llm-request`/`-reply`/`-dlq`, `ap-northeast-2`, 계정 416121583617)로 큐 E2E 완료(2026-09-13)**:
    `scripts/stub_backend.py`(가짜 백엔드)로 실제 요청 큐에 발행 → `ubidict-py` 컨슈머가 실제로 수신·파싱 → 실제 Gemini 호출(extract: gemini-3.5-flash, 5.2s / contrast: 2.7s) → 실제 응답 큐로 결과 발행 → stub이 수신 확인. 멱등성도 확인: 같은 jobId를 다시 발행하니 `이미 처리됨(중복 배달) — 건너뛰고 메시지만 삭제` 로그와 함께 Gemini 재호출 없이 스킵됨(MySQL `processed_jobs`에 기록 남음, 로컬 MySQL은 포트 충돌 회피로 3308 사용 — `docker-compose.local.yml`/`.env.example` 참고). `main.py`에 `logging.basicConfig`를 추가해 `queue_consumer.py`의 INFO 로그가 실제로 보이도록 고침.
    **단, 이건 진짜 백엔드가 아니라 스텁으로 한 것** — `final/backend`엔 SQS 코드·요청 엔드포인트가 전혀 없음을 확인함(D-24/D-34/D-35 계획만 있고 미착수, `REQ-EXT-*`/`REQ-CHK-*` 상태 "대기"). 아래 미결 사항 참고.
- [x] 10. **문서·사전집을 ID 기반으로 백엔드 DB에서 직접 읽는 것으로 전환(2026-09-14)**:
  - `final/backend`를 조사해 실제 스키마 확보(추측 아님, Flyway 마이그레이션 원문 확인) — `document`(메타)+`document_version.body`(본문, TEXT 최대 10,000자), `term(dictionary_id, preferred_form, english_name, definition)`(`synonyms` 컬럼 없음, test/SPEC.md D-22와 같은 결론). `department`는 대응 컬럼이 없어 빈 문자열로 채움
  - `app/backend_db.py` 신규 — `fetch_documents`/`fetch_existing_terms`/`fetch_dictionary_entries`(읽기 전용, 백엔드 소유 테이블에 안 씀)
  - `app/job_schema.py` 신규 — `ExtractJobRequest`/`ContrastJobRequest`(jobId/workspaceId/dictionaryVersionNo/dictionaryId/documentIds/accessToken/mode)
  - `app/service.py`에 `run_extract_job`/`run_contrast_job` 추가 — mode="stub"이면 DB·Gemini 다 스킵하고 2.5초 뒤 빈 결과, "real"이면 DB 조회 후 기존 `run_extract`/`run_contrast`에 위임(판정 로직 자체는 안 바뀜)
  - `app/queue_consumer.py`·`app/main.py`(`/jobs/extract`·`/jobs/contrast` 신설) 둘 다 이 함수를 쓰도록 교체. 결과에 `accessToken`을 그대로 에코(`QueueResultEnvelope`에 필드 추가)
  - **로컬 DB를 `final/backend`의 MySQL로 통합** — 우리만의 `ubidict-py-mysql-1`(docker-compose) 제거, `docker-compose.local.yml`은 이제 LocalStack(SQS)만 남김. `.env.example`도 이에 맞춰 갱신
  - 실측으로 잡은 버그: `backend_db.py`/`idempotency.py`의 pymysql 연결에 `charset="utf8mb4"`를 명시 안 하면 한글이 깨짐(mojibake) — 실제 데이터 넣고 조회해보다가 발견, 수정 완료
  - **검증**: 실제 `backend-mysql-1`에 테스트용 workspace/document/document_version/dictionary/term 행을 임시로 넣고(Python pymysql로 — 처음에 bash heredoc으로 넣었다가 클라이언트 인코딩 문제로 한 번 깨진 데이터가 들어가는 걸 발견해 지움) `/jobs/extract`(stub·real)·`/jobs/contrast`(real)를 HTTP로 직접 호출해 끝까지 확인: stub은 2.5초 뒤 빈 결과+accessToken 에코, real은 실제 DB에서 읽은 본문으로 실제 Gemini 호출(extract 5.5s, contrast 5.1s) 성공. `scripts/stub_backend.py --mode stub`으로 실제 SQS 큐를 통한 전체 흐름도 재확인(큐→컨슈머→DB→SQS 응답). 테스트 데이터는 전부 삭제해 원상복구함
  - 단위 테스트 추가: `tests/test_backend_db.py`(컬럼 매핑), `tests/test_service_job.py`(stub이 DB/Gemini 안 부르는지, real이 fetch 결과를 그대로 전달하는지), `tests/test_queue_consumer.py`를 새 job 스키마 기준으로 재작성 — 전부 통과(23개)
- [ ] 11. 미결 사항(아래 "다음에 할 것" 참고)

## 다음에 할 것 / 미결 사항

- [x] ~~`idempotency.py`·`queue_consumer.py` 단위 테스트(모킹) 작성~~ — 2026-09-14 완료. `tests/{test_idempotency,test_queue_consumer,test_queue_schema,test_main}.py` — boto3/pymysql/Gemini 전부 모킹, 15개 전부 통과(로컬 + GitHub Actions 둘 다 확인)
- [x] ~~실제 AWS 자격증명·SQS 큐가 생기면 `queue_consumer.py` 통합 테스트~~ — 2026-09-13 완료(위 참고). 큐 3개는 실제로 존재(표준 큐, DLQ `maxReceiveCount=3` 연결됨, `VisibilityTimeout=900s`) — `final/docs`가 서술한 "FIFO+MessageGroupId" 방향과 다르지만 표준 큐로도 지금 문제없이 동작함
- [x] ~~MySQL 실제 인스턴스 연결 확인~~ — 2026-09-14부터 우리만의 로컬 MySQL(포트 3308)은 제거하고 `final/backend`의 MySQL(`backend-mysql-1`)을 그대로 씀(위 10번 참고). **실제 운영 RDS는 별도**(이건 로컬 개발용일 뿐)
- [x] ~~CI 파이프라인 확정~~ — 2026-09-14, **GitHub Actions로 확정**(CodeBuild 아님). `.github/workflows/ubidict-py-ci.yml`(lint+test, PR/push마다) 실제로 push해서 GitHub에서 통과 확인(run 34800209082). `.github/workflows/ubidict-py-deploy.yml`(main push 시 ECR→ECS→CodeDeploy)도 같이 작성했지만 **아직 실제로 성공할 수 없음** — 참조하는 AWS 인프라(ECS 클러스터/서비스, CodeDeploy 애플리케이션/배포 그룹, ECR 리포지토리, IAM OIDC 역할, Secrets Manager)가 하나도 없음. `deploy/buildspec.yml`(CodeBuild 템플릿)은 제거함
- [ ] **백엔드가 실제로 이 기능(용어 추출·사전집 대조 요청)을 구현하면**: `app/queue_schema.py`의 봉투 구조가 실제 계약과 맞는지 재검토 — `final/backend`는 현재 `DI-5`(`POST /api/draft-dictionaries/extractions`)·`DD-5`(`POST /api/draft-documents/checks`)가 미착수라 아직 맞출 대상이 없음. 방향성은 알려짐: `PENDING/RUNNING/SUCCEEDED/FAILED` 작업 테이블 + 폴링, 이벤트는 "불변 record, 식별자/원시값/값객체/시각 타입만"(`final/docs/ARCHITECTURE.md`) — `queue_schema.py`의 초안 봉투가 이 규칙과 크게 어긋나진 않아 보이나 필드명은 재조정 필요할 가능성 높음
- [ ] 표준 큐 vs FIFO 큐 — `final/docs`는 FIFO+`MessageGroupId=workspaceId`를 설계 방향으로 서술하는데 실제 provisioned 큐는 표준 큐다. 어느 쪽이 맞을지 인프라/백엔드 팀 확인 필요(지금 코드는 표준 큐 기준이라 FIFO로 바뀌면 `queue_consumer.py`의 `send_message` 호출에 `MessageGroupId`/`MessageDeduplicationId` 추가 필요)
- [ ] 실패 시 응답 큐에 매번 FAILED를 발행하는 지금 방식이 백엔드 쪽에서 괜찮은지 확인(재시도마다 중복 발행 가능 — `queue_consumer.py` 주석 참고)
- [ ] OpenTelemetry 계측 여부/방식 결정되면 추가(지금은 의존성 자체를 안 넣음)
- [ ] **`accessToken`을 왜 돌려받는지 확인** — 지금은 검증 없이 그대로 에코만 한다(`reference/backend/README.md` 참고). 백엔드가 이걸로 뭘 하려는지 알면 처리 방식이 맞는지 재확인 가능
- [ ] `job.workspaceId`/`dictionaryVersionNo`가 실제로 어떻게 쓰이는지 — 지금은 `fetch_documents`/`fetch_existing_terms`가 `documentIds`/`dictionaryId`만으로 조회하고 `workspaceId`는 안 씀(응답 조립에만 사용). 백엔드가 워크스페이스 소속 검증까지 기대하는지 확인 필요
- [ ] 운영 DB 접속 계정 — 지금은 로컬처럼 백엔드와 같은 계정(`ubidict`)을 가정했다. 운영에서는 `app/backend_db.py`용으로 **읽기 전용 별도 계정**을 만드는 게 안전(쓰기 권한 자체가 없으면 실수로 백엔드 테이블에 쓰는 사고를 원천 차단)
- [ ] 실제 ECS 클러스터·서비스·ALB·IAM 역할·SQS 큐·RDS(MySQL) 프로비저닝 — Terraform/CDK로 별도 진행(이번 범위 밖)
- [ ] `.env`의 실제 운영 값(Secrets Manager/SSM 연동) 정리 — 지금 `taskdef.json`은 `${VAR}` placeholder로 남겨둠(envsubst로 채움, deploy 워크플로 참고)
- [ ] **`ubidict-py-deploy.yml`이 실제로 돌려면 리포에 등록해야 하는 것**(전부 인프라 준비 후):
  - Secrets: `AWS_DEPLOY_ROLE_ARN`(GitHub OIDC 신뢰 IAM 역할), `TASK_EXECUTION_ROLE_ARN`, `TASK_ROLE_ARN`, `SECRETS_MANAGER_ARN`
  - Variables: `AWS_REGION`(기본값 `ap-northeast-2`로 이미 둠), `ECR_REPOSITORY`(기본값 `ubidict-py`), `ECS_CLUSTER`, `ECS_SERVICE`, `CODEDEPLOY_APPLICATION`, `CODEDEPLOY_DEPLOYMENT_GROUP`
  - GitHub 저장소 ↔ AWS IAM 간 OIDC 신뢰 관계 설정(장기 액세스 키 안 씀)
