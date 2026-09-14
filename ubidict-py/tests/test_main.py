"""`/health`만 확인한다 — `/extract`·`/contrast`는 실제 Gemini 호출이 필요해
CI에 안 올린다(비용·비결정성). 그 두 경로는 2026-09-13에 실제 SQS+Gemini로
수동 검증했다(task.md 참고). 여기서는 `SQS_REQUEST_QUEUE_URL`이 없을 때
컨슈머가 조용히 시작을 건너뛰고 앱이 정상 기동하는지만 확인한다.
"""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

from app.main import app


def _clear_model_env(monkeypatch):
    # 로컬 개발용 .env가 GEMINI_MODEL_1.._N을 갖고 있을 수 있고, 그건 단수
    # GEMINI_MODEL보다 우선한다(load_default_model_chain) — 테스트가 로컬
    # .env 내용에 좌우되지 않도록 관련 변수를 전부 지우고 시작한다.
    for key in list(os.environ):
        if key.startswith("GEMINI_MODEL"):
            monkeypatch.delenv(key, raising=False)


def test_health_ok_without_any_queue_or_gemini_config(monkeypatch):
    monkeypatch.delenv("SQS_REQUEST_QUEUE_URL", raising=False)
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-model")

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["model"] == "gemini-test-model"


def test_health_falls_back_to_unset_when_no_model_configured(monkeypatch):
    monkeypatch.delenv("SQS_REQUEST_QUEUE_URL", raising=False)
    _clear_model_env(monkeypatch)

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.json()["model"] == "unset"
