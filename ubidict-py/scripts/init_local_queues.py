"""Spring ↔ FastAPI 연동용 LocalStack 요청 큐를 만들고 URL을 출력한다.

사용법:
    docker compose -f docker-compose.local.yml up -d   # LocalStack 먼저 기동
    python scripts/init_local_queues.py                # 이 스크립트 실행

Spring은 ``lovebug-llm-request``로 요청을 발행하고 FastAPI는 Spring의 HTTP
콜백 엔드포인트로 결과를 돌려준다. 응답용 SQS 큐는 사용하지 않는다.
"""

from __future__ import annotations

import boto3

_ENDPOINT_URL = "http://localhost:4566"
_REGION = "ap-northeast-2"


def main() -> None:
    client = boto3.client(
        "sqs",
        endpoint_url=_ENDPOINT_URL,
        region_name=_REGION,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    request_queue_url = client.create_queue(QueueName="lovebug-llm-request")["QueueUrl"]

    print("아래 값을 .env에 그대로 붙여넣으세요:\n")
    print(f"SQS_REQUEST_QUEUE_URL={request_queue_url}")
    print(f"SQS_ENDPOINT_URL={_ENDPOINT_URL}")
    print("BACKEND_CALLBACK_BASE_URL=http://localhost:8080")
    print("AWS_ACCESS_KEY_ID=test")
    print("AWS_SECRET_ACCESS_KEY=test")


if __name__ == "__main__":
    main()
