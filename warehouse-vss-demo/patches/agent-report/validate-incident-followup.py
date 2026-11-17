#!/usr/bin/env python3
"""Bounded report-to-follow-up acceptance using the existing Warehouse Agent.

Run inside the Agent Pod. Execution generates one report and invokes existing
models; it does not change cameras, rules, recording or Kubernetes resources.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.request
from uuid import uuid4


MAX_BYTES = 2_000_000


def visible(text: str) -> str:
    return re.sub(r"<agent-think>.*?</agent-think>|<think>.*?</think>", "", text, flags=re.S).strip()


def chat(question: str, conversation: str, timeout: int) -> str:
    payload = {"messages": [{"role": "user", "content": question}], "stream": False}
    request = urllib.request.Request(
        "http://127.0.0.1:8000/v1/chat/completions",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Conversation-Id": conversation,
                 "X-Timezone": "America/Los_Angeles"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError("Agent request did not return HTTP 200")
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Agent response exceeds bounded size")
    content = json.loads(raw)["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise ValueError("Agent response is not text")
    return visible(content)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incident-id", required=True)
    parser.add_argument("--sensor", required=True)
    parser.add_argument("--question", default="What is the color of clothes of the person who is not wearing a hard hat?")
    parser.add_argument("--expected-word", action="append", default=[])
    parser.add_argument("--video-question", help="Optional detail absent from stored evidence, to exercise exact-clip analysis")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.incident_id):
        parser.error("An exact 40-character incident ID is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", args.sensor):
        parser.error("An exact safe sensor name is required")
    if not 30 <= args.timeout <= 600 or len(args.question) > 2000:
        parser.error("Invalid bounded timeout or question")
    if not args.execute:
        print("PLAN ONLY: generate one exact-incident report, then same-chat follow-ups and a separate-chat check")
        return 0
    conversation = str(uuid4())
    print(json.dumps({"check": "started", "conversation_id": conversation}), flush=True)
    report = chat(f"Generate a report for incident {args.incident_id} with sensor id {args.sensor}.", conversation, args.timeout)
    if args.incident_id not in report or args.sensor not in report or "[PDF Report](" not in report:
        raise ValueError("Fresh report did not return its expected identity and artifact")
    answer = chat(args.question, conversation, args.timeout)
    print(json.dumps({"check": "followup_observed", "answer": answer}, ensure_ascii=False), flush=True)
    if not any(marker in answer for marker in (
        "Source: stored incident assessment/report", "Source: analysis of the same incident clip"
    )):
        raise ValueError("Expected incident-bound follow-up was not routed")
    if args.incident_id not in answer or args.sensor not in answer:
        raise ValueError("Follow-up identity does not match its report")
    if not all(word.casefold() in answer.casefold() for word in args.expected_word):
        raise ValueError("Follow-up lacks an expected source-supported term")
    print(json.dumps({"check": "report_to_followup", "status": "pass", "conversation_id": conversation,
                      "incident_id": args.incident_id, "answer": answer}, ensure_ascii=False), flush=True)
    other_sensor = "Camera_03" if args.sensor != "Camera_03" else "Camera_02"
    mismatch = chat(f"What color clothes is the person wearing in {other_sensor}?", conversation, args.timeout)
    if "I have not switched cameras" not in mismatch:
        raise ValueError("Explicit other-camera guard failed")
    print(json.dumps({"check": "other_camera_guard", "status": "pass"}), flush=True)
    separate = chat(args.question, str(uuid4()), args.timeout)
    if args.incident_id in separate or any(marker in separate for marker in (
        "Source: stored incident", "Source: analysis of the same incident clip"
    )):
        raise ValueError("Another conversation inherited incident context")
    print(json.dumps({"check": "separate_conversation", "status": "pass"}), flush=True)
    if args.video_question:
        analysed = chat(args.video_question, conversation, args.timeout)
        if "Source: analysis of the same incident clip" not in analysed or args.incident_id not in analysed:
            raise ValueError("Exact-clip visual follow-up did not succeed")
        print(json.dumps({"check": "exact_clip_followup", "status": "pass", "answer": analysed}, ensure_ascii=False), flush=True)
    print("PASS: report-follow-up acceptance; browser layout and visual factual review remain separate", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
