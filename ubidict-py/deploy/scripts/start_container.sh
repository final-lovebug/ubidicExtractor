#!/bin/bash
set -euo pipefail

# AfterInstall 훅 — 이미지를 받아 컨테이너를 새로 띄운다.
# 시크릿은 여기(인스턴스 자신의 IAM 역할, lovebug-ec2-fastapi)에서만 읽는다.
# GitHub Actions나 이 스크립트를 작성한 세션을 거치지 않는다 — set -x는
# 절대 켜지 않는다(값이 CodeDeploy 로그에 그대로 찍힌다).

REGION=ap-northeast-2
REGISTRY=416121583617.dkr.ecr.ap-northeast-2.amazonaws.com
IMAGE_REPO="$REGISTRY/lovebug/fastapi"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAG=$(cat "$SCRIPT_DIR/../IMAGE_TAG")
echo "deploying tag: $TAG"

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

docker pull "$IMAGE_REPO:$TAG"

# ── /prod/llm/* 전부를 그대로 환경변수로 만든다 ──────────────────────────
# GEMINI_API_KEY_1(_2, ...)·GEMINI_MODEL_1(_2, ...) 등 몇 개를 등록했든
# 파라미터 이름의 마지막 세그먼트가 그대로 환경변수 이름이 된다
# (test/의 D-38~D-40 키·모델 폴백 체인 규칙과 그대로 맞물린다).
ENV_ARGS=()
while IFS=$'\t' read -r NAME VALUE; do
  [ -z "$NAME" ] && continue
  ENV_ARGS+=(-e "${NAME##*/}=${VALUE}")
done < <(aws ssm get-parameters-by-path --path /prod/llm --recursive --with-decryption \
  --region "$REGION" --query "Parameters[].[Name,Value]" --output text 2>/dev/null || true)

if [ "${#ENV_ARGS[@]}" -eq 0 ]; then
  echo "경고: /prod/llm/* 에 파라미터가 하나도 없다 — GEMINI_API_KEY를 아직 안 넣은 상태로 보인다." >&2
  echo "       (task.md '사용자가 할 일' 참고) 컨테이너는 뜨지만 real 모드 호출은 전부 실패한다." >&2
fi

# ── 백엔드와 공유하는 RDS 접속 정보 ──────────────────────────────────────
# /lovebug/rds/url은 스프링이 쓰는 JDBC 형식(jdbc:mysql://host:port/db?...)
# 이라 그대로는 못 쓴다 — pymysql이 쓰는 discrete 환경변수로 쪼갠다.
RDS_URL=$(aws ssm get-parameter --name /lovebug/rds/url --with-decryption \
  --region "$REGION" --query Parameter.Value --output text)
RDS_USER=$(aws ssm get-parameter --name /lovebug/rds/username --with-decryption \
  --region "$REGION" --query Parameter.Value --output text)
RDS_PASSWORD=$(aws ssm get-parameter --name /lovebug/rds/password --with-decryption \
  --region "$REGION" --query Parameter.Value --output text)

HOSTPORT_DB="${RDS_URL#jdbc:mysql://}"
HOSTPORT="${HOSTPORT_DB%%/*}"
DB_AND_QUERY="${HOSTPORT_DB#*/}"
DB_NAME="${DB_AND_QUERY%%\?*}"
MYSQL_HOST="${HOSTPORT%%:*}"
MYSQL_PORT="${HOSTPORT##*:}"

docker rm -f ubidict-py 2>/dev/null || true

docker run -d --name ubidict-py \
  --restart unless-stopped \
  -p 8000:8000 \
  --memory 500m \
  --log-driver json-file \
  --log-opt max-size=100m \
  --log-opt max-file=3 \
  -e AWS_REGION="$REGION" \
  -e SQS_REQUEST_QUEUE_URL="https://sqs.$REGION.amazonaws.com/416121583617/lovebug-llm-request" \
  -e SQS_REPLY_QUEUE_URL="https://sqs.$REGION.amazonaws.com/416121583617/lovebug-llm-reply" \
  -e MYSQL_HOST="$MYSQL_HOST" \
  -e MYSQL_PORT="$MYSQL_PORT" \
  -e MYSQL_DATABASE="$DB_NAME" \
  -e MYSQL_USER="$RDS_USER" \
  -e MYSQL_PASSWORD="$RDS_PASSWORD" \
  -e GEMINI_MODEL_1=gemini-3.5-flash \
  "${ENV_ARGS[@]}" \
  "$IMAGE_REPO:$TAG"

docker image prune -af --filter "until=168h" || true
