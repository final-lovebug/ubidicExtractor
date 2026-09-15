#!/bin/bash
# ApplicationStop 훅 — 기존 ubidict-py 컨테이너를 내린다. 컨테이너가 없는
# 최초 배포에서도 실패하지 않도록 || true로 무시한다(final/deploy와 동일).
docker stop --time 30 ubidict-py 2>/dev/null || true
docker rm ubidict-py 2>/dev/null || true
exit 0
