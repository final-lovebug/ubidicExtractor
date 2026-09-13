# 최소 실행 이미지 — uvicorn으로 FastAPI 앱(HTTP + 내장 SQS 컨슈머)을 띄운다.
# 실제 ECS 클러스터/서비스/IAM 등 인프라 프로비저닝은 이 파일의 범위 밖이다
# (task.md "배포 전략" 참고 — Terraform/CDK는 별도 작업).

FROM python:3.11-slim

WORKDIR /app

# 의존성만 먼저 복사해 레이어 캐시를 살린다.
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .

COPY app/ ./app/
COPY prompts/ ./prompts/
COPY db/ ./db/

# .env는 이미지에 넣지 않는다 — 런타임에 ECS 태스크 정의의 환경변수/AWS Secrets
# Manager·SSM Parameter Store로 주입한다(taskdef.json 참고).

EXPOSE 8000

# out/·.cache/·logs/는 컨테이너 로컬(재시작하면 사라짐) — 사용량 로그는
# 표준출력으로도 나가므로(usage_log.py) CloudWatch에서 영구히 확인 가능하다.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
