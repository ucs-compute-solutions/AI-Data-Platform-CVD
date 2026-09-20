#!/usr/bin/env python3
"""Run bounded, non-secret smoke tests against three local NVIDIA NIM APIs."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


DEFAULT_EMBEDDING_BASE = "http://embedding.nims.svc:8029"
DEFAULT_LLM_BASE = "http://llm-nemotron-35-lightning.nims.svc:8025"
DEFAULT_RERANKER_BASE = "http://reranker.nims.svc:8028"

DEFAULT_EMBEDDING_MODEL = "nvidia/llama-nemotron-embed-1b-v2"
DEFAULT_LLM_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"
DEFAULT_RERANKER_MODEL = "nvidia/llama-3.2-nv-rerankqa-1b-v2"
DEFAULT_MARKER = "CVD_LOCAL_NIM_OK"


def request_json(
    method: str, url: str, payload: dict | None = None, timeout: float = 120.0
) -> tuple[int, dict, float]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{url}: HTTP {exc.code}") from None
    except Exception as exc:
        raise RuntimeError(f"{url}: {type(exc).__name__}") from None
    return status, result, time.monotonic() - started


def model_ids(base: str) -> tuple[int, list[str]]:
    status, body, _ = request_json("GET", f"{base}/v1/models")
    return status, [str(item.get("id", "")) for item in body.get("data", [])]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--embedding-base", default=DEFAULT_EMBEDDING_BASE)
    parser.add_argument("--llm-base", default=DEFAULT_LLM_BASE)
    parser.add_argument("--reranker-base", default=DEFAULT_RERANKER_BASE)
    parser.add_argument("--embedding-model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--llm-model", default=DEFAULT_LLM_MODEL)
    parser.add_argument("--reranker-model", default=DEFAULT_RERANKER_MODEL)
    parser.add_argument("--expected-dimensions", type=int, default=2048)
    parser.add_argument("--marker", default=DEFAULT_MARKER)
    parser.add_argument(
        "--skip-reranker",
        action="store_true",
        help="validate embedding and LLM only while the reranker replica is intentionally zero",
    )
    args = parser.parse_args()
    failures: list[str] = []

    print("Stage 1: model discovery")
    models = [
        ("embedding", args.embedding_base, args.embedding_model),
        ("llm", args.llm_base, args.llm_model),
    ]
    if not args.skip_reranker:
        models.append(("reranker", args.reranker_base, args.reranker_model))
    for name, base, expected in models:
        try:
            status, models = model_ids(base)
            found = expected in models
            print(f"{name}: HTTP {status}; expected model present: {found}")
            if status != 200 or not found:
                failures.append(f"{name} model discovery")
        except RuntimeError as exc:
            print(f"{name}: FAIL ({exc})")
            failures.append(f"{name} model discovery")

    print("Stage 2: embedding")
    try:
        status, body, elapsed = request_json(
            "POST",
            f"{args.embedding_base}/v1/embeddings",
            {
                "model": args.embedding_model,
                "input": ["CVD local NIM validation"],
                "input_type": "query",
                "truncate": "END",
            },
        )
        results = body.get("data", [])
        dimensions = len(results[0].get("embedding", [])) if results else 0
        passed = (
            status == 200
            and len(results) == 1
            and dimensions == args.expected_dimensions
        )
        print(
            f"HTTP {status}; results: {len(results)}; dimensions: {dimensions}; "
            f"elapsed: {elapsed:.2f}s; passed: {passed}"
        )
        if not passed:
            failures.append("embedding inference")
    except RuntimeError as exc:
        print(f"FAIL ({exc})")
        failures.append("embedding inference")

    print("Stage 3: reranking")
    if args.skip_reranker:
        print("SKIP: reranker replica is intentionally zero in NVIDIA Search critic mode")
    else:
        try:
            status, body, elapsed = request_json(
                "POST",
                f"{args.reranker_base}/v1/ranking",
                {
                    "model": args.reranker_model,
                    "query": {"text": "Which system stores vector embeddings?"},
                    "passages": [
                        {"text": "VAST DataBase stores vector embeddings."},
                        {"text": "Knative schedules serverless workloads."},
                    ],
                    "truncate": "END",
                },
            )
            rankings = body.get("rankings", [])
            indices = [item.get("index") for item in rankings]
            passed = status == 200 and indices == [0, 1]
            print(
                f"HTTP {status}; ranking indices: {indices}; relevant first: "
                f"{bool(indices) and indices[0] == 0}; elapsed: {elapsed:.2f}s; "
                f"passed: {passed}"
            )
            if not passed:
                failures.append("reranking inference")
        except RuntimeError as exc:
            print(f"FAIL ({exc})")
            failures.append("reranking inference")

    print("Stage 4: LLM completion")
    try:
        status, body, elapsed = request_json(
            "POST",
            f"{args.llm_base}/v1/chat/completions",
            {
                "model": args.llm_model,
                "messages": [
                    {
                        "role": "user",
                        "content": f"Reply with exactly this marker: {args.marker}",
                    }
                ],
                "temperature": 0,
                "max_tokens": 128,
                "stream": False,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout=300.0,
        )
        choices = body.get("choices", [])
        content = ""
        if choices:
            content = str(choices[0].get("message", {}).get("content", ""))
        marker_present = args.marker in content
        passed = status == 200 and bool(choices) and marker_present
        print(
            f"HTTP {status}; choices: {len(choices)}; marker present: "
            f"{marker_present}; elapsed: {elapsed:.2f}s; passed: {passed}"
        )
        if not passed:
            failures.append("LLM inference")
    except RuntimeError as exc:
        print(f"FAIL ({exc})")
        failures.append("LLM inference")

    if failures:
        print("FAIL: " + ", ".join(failures))
        return 1
    if args.skip_reranker:
        print("PASS: embedding and LLM local NIM APIs; reranker intentionally skipped")
    else:
        print("PASS: embedding, LLM, and reranker local NIM APIs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
