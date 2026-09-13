"""가짜 백엔드 — 실제 백엔드 코드가 오기 전까지 이걸로 큐 흐름 전체
(요청 큐 → ubidict-py 소비·처리 → 응답 큐)를 로컬에서 검증한다.

`app/queue_schema.py`의 봉투가 초안이라, 이 스크립트가 보내는 메시지 형식도
그 초안 그대로다 — 백엔드 실제 계약이 오면 이 스크립트도 같이 고친다
(또는 통합 테스트용으로 계속 남겨둬도 된다).

사용법 (`ubidict-py`가 이미 떠 있고 `.env`에 SQS_* 값이 채워진 상태에서):
    python scripts/stub_backend.py --type extract
    python scripts/stub_backend.py --type contrast
    python scripts/stub_backend.py --type extract --payload-file my_request.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

import boto3
from dotenv import load_dotenv

load_dotenv()


def _sample_extract_payload() -> dict:
    return {
        "jobId": str(uuid.uuid4()),
        "workspaceId": "local-stub-workspace",
        "dictionaryVersionNo": 1,
        "existingTerms": [],
        "documents": [
            {
                "documentId": "d-stub-1",
                "title": "스텁 문서",
                "department": "개발",
                "content": (
                    "이용 보류 상태의 계정은 결제 실패가 3회 누적된 경우다. "
                    "이용 보류와 별개로 휴면 전환도 있다."
                ),
            }
        ],
    }


def _sample_contrast_payload() -> dict:
    return {
        "jobId": str(uuid.uuid4()),
        "workspaceId": "local-stub-workspace",
        "dictionaryVersionNo": 1,
        "dictionary": [
            {
                "termId": "t-001",
                "preferredForm": "구독자",
                "englishName": "Subscriber",
                "synonyms": [],
                "definition": "이 서비스를 이용하는 계정의 주체",
            }
        ],
        "documents": [
            {
                "documentId": "d-stub-1",
                "title": "스텁 문서",
                "department": "마케팅",
                "content": "이번 달 이용자 대상 혜택을 안내드립니다.",
            }
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--type", choices=["extract", "contrast"], required=True)
    parser.add_argument("--payload-file", type=Path, help="직접 만든 요청 JSON 파일(옵션, 안 주면 내장 예시 사용)")
    parser.add_argument("--timeout", type=int, default=60, help="응답 대기 최대 초(기본 60)")
    args = parser.parse_args()

    request_queue_url = os.getenv("SQS_REQUEST_QUEUE_URL")
    reply_queue_url = os.getenv("SQS_REPLY_QUEUE_URL")
    endpoint_url = os.getenv("SQS_ENDPOINT_URL")
    if not request_queue_url or not reply_queue_url:
        sys.exit(".env에 SQS_REQUEST_QUEUE_URL/SQS_REPLY_QUEUE_URL이 없습니다 — scripts/init_local_queues.py를 먼저 실행하세요.")

    client_kwargs: dict = {"region_name": os.getenv("AWS_REGION")}
    if endpoint_url:
        client_kwargs["endpoint_url"] = endpoint_url
    client = boto3.client("sqs", **client_kwargs)

    if args.payload_file:
        payload = json.loads(args.payload_file.read_text(encoding="utf-8"))
    else:
        payload = _sample_extract_payload() if args.type == "extract" else _sample_contrast_payload()

    job_id = payload["jobId"]
    envelope = {"jobId": job_id, "type": args.type, "replyQueueUrl": reply_queue_url, "payload": payload}

    client.send_message(QueueUrl=request_queue_url, MessageBody=json.dumps(envelope, ensure_ascii=False))
    print(f"요청 전송: jobId={job_id}, type={args.type}")
    print("응답 큐를 폴링합니다 (ubidict-py 컨슈머가 떠 있어야 합니다)...")

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        response = client.receive_message(QueueUrl=reply_queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=5)
        for message in response.get("Messages", []):
            body = json.loads(message["Body"])
            if body.get("jobId") != job_id:
                continue  # 다른 테스트가 쓰는 메시지일 수 있으니 삭제하지 않고 둔다
            print("\n=== 응답 수신 ===")
            print(json.dumps(body, ensure_ascii=False, indent=2))
            client.delete_message(QueueUrl=reply_queue_url, ReceiptHandle=message["ReceiptHandle"])
            return

    print(f"{args.timeout}초 안에 응답을 못 받았습니다 — ubidict-py가 떠 있는지, .env의 큐 설정이 맞는지 확인하세요.")


if __name__ == "__main__":
    main()
