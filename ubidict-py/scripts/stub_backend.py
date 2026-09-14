"""가짜 백엔드 — 실제 백엔드 코드가 오기 전까지 이걸로 큐 흐름 전체
(요청 큐 → ubidict-py 소비·처리 → 응답 큐)를 로컬에서 검증한다.

`app/queue_schema.py`의 봉투와 `app/job_schema.py`의 payload가 둘 다 초안이라,
이 스크립트가 보내는 메시지 형식도 그 초안 그대로다(2026-09-14, documentIds/
dictionaryId 기반으로 바뀜 — 문서 본문을 인라인으로 안 보낸다) — 백엔드 실제
계약이 오면 이 스크립트도 같이 고친다(또는 통합 테스트용으로 남겨둬도 된다).

기본값(`--mode stub`)은 DB에 아무 데이터가 없어도 동작한다 — ubidict-py가
DB·Gemini를 아예 안 부르고 빈 결과를 돌려주기 때문이다. `--mode real`로
실제 처리까지 보려면 `--document-ids`/`--dictionary-id`가 실제 backend DB
(document/term 테이블)에 존재하는 값이어야 한다.

사용법 (`ubidict-py`가 이미 떠 있고 `.env`에 SQS_* 값이 채워진 상태에서):
    python scripts/stub_backend.py --type extract
    python scripts/stub_backend.py --type contrast
    python scripts/stub_backend.py --type extract --mode real --document-ids 1 2 --dictionary-id 1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid

import boto3
from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--type", choices=["extract", "contrast"], required=True)
    parser.add_argument("--mode", choices=["stub", "real"], default="stub", help="기본 stub — DB/Gemini 안 부름")
    parser.add_argument("--workspace-id", type=int, default=1)
    parser.add_argument("--dictionary-id", type=int, default=1)
    parser.add_argument("--dictionary-version-no", type=int, default=1)
    parser.add_argument("--document-ids", type=int, nargs="+", default=[1], help="documentId 목록(공백 구분)")
    parser.add_argument("--access-token", default="local-stub-access-token", help="그냥 에코되는 값, 검증 안 됨")
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

    job_id = str(uuid.uuid4())
    payload = {
        "jobId": job_id,
        "workspaceId": args.workspace_id,
        "dictionaryVersionNo": args.dictionary_version_no,
        "dictionaryId": args.dictionary_id,
        "documentIds": args.document_ids,
        "accessToken": args.access_token,
        "mode": args.mode,
    }
    envelope = {"jobId": job_id, "type": args.type, "replyQueueUrl": reply_queue_url, "payload": payload}

    client.send_message(QueueUrl=request_queue_url, MessageBody=json.dumps(envelope, ensure_ascii=False))
    print(f"요청 전송: jobId={job_id}, type={args.type}, mode={args.mode}")
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
