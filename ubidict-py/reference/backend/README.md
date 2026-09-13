# 백엔드 연동 참고 자료 — 아직 비어있음

이 폴더는 백엔드(Spring) 팀의 큐 연동 코드/설정이 준비되면 참고용으로 받아 넣는 자리다.
지금은 `app/queue_schema.py`의 메시지 봉투가 **초안**이고, 아래 내용이 확인되면
다시 맞춰야 한다.

## 확인 필요한 것들

- [ ] SQS 요청 큐 이름/ARN, 응답 큐 이름/ARN (리전 포함)
- [ ] 요청 메시지의 실제 필드 구조 — 지금 초안(`QueueTaskEnvelope`)은
      `{jobId, type, replyQueueUrl, payload}` 형태로 가정했다. 실제로 백엔드가
      이렇게 보내는지, 아니면 다른 봉투 구조(예: SNS 팬아웃, 메시지 속성으로
      타입 구분 등)를 쓰는지 확인
- [ ] `type`(`"extract"`|`"contrast"`) 구분을 메시지 바디에 넣을지, SQS
      메시지 속성(MessageAttributes)으로 넣을지, 아니면 큐 자체를 종류별로
      나눌지(예: extract 전용 큐 vs contrast 전용 큐)
- [ ] 응답을 매번 다른 큐(`replyQueueUrl`)로 보낼지, 고정된 응답 큐 하나로
      보낼지 — 지금 초안은 메시지에 실린 `replyQueueUrl`을 우선하고 없으면
      `.env`의 `SQS_REPLY_QUEUE_URL`로 폴백하게 해뒀다
- [ ] 인증 방식 — ECS 태스크 역할(IAM Role)로 SQS/자격증명 없이 접근하는 게
      기본 전제. 다른 방식이면 조정 필요
- [ ] 실패 시 정책 — `queue_consumer.py`는 지금 실패해도 매번 FAILED 응답을
      즉시 발행한다(재시도마다 중복 발행 가능). 백엔드가 "가장 최근 상태만
      신뢰"하는 방식인지, 아니면 최종 실패(DLQ 이동) 시점에만 알림받고
      싶은지 확인 필요
- [ ] 멱등성 — `app/idempotency.py`가 jobId 기준으로 중복 처리를 막는다.
      jobId가 정말 전역에서 유일한지(백엔드가 UUID로 발급하는지) 확인

## 여기에 받아 넣을 것 (예시)

- 백엔드 쪽 큐 프로듀서/컨슈머 코드 스니펫(Spring Cloud AWS 등)
- 실제 메시지 스키마 정의(JSON Schema·DTO 클래스 등)
- 인프라(Terraform/CDK) 중 SQS·IAM 관련 부분만 발췌
