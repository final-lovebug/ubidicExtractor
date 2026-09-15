#!/bin/bash
set -uo pipefail

# ValidateService 훅 — 컨테이너가 실제로 요청을 받을 수 있는지 확인한 뒤에만
# CodeDeploy가 이 인스턴스를 타깃그룹에 다시 등록한다(final/deploy/validate.sh와 동일 패턴).
for i in $(seq 1 15); do
  if curl -fsS --max-time 5 http://localhost:8000/health >/dev/null 2>&1; then
    echo "healthy after $((i * 10))s"
    exit 0
  fi
  sleep 10
done

echo "health check failed"
docker logs --tail 200 ubidict-py 2>&1 || true
exit 1
