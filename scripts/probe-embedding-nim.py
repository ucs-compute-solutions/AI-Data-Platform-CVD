#!/usr/bin/env python3
"""Run bounded embedding requests against a service or individual pod endpoints."""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", action="append", required=True)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument(
        "--input-type",
        choices=("query", "passage"),
        default="query",
    )
    parser.add_argument(
        "--show-error",
        action="store_true",
        help="Print a redacted, truncated NIM error message for failed requests",
    )
    parser.add_argument(
        "--model",
        default="nvidia/llama-nemotron-embed-1b-v2",
    )
    parser.add_argument("--expected-dimensions", type=int, default=2048)
    return parser.parse_args()


def redact(value: str) -> str:
    patterns = (
        r"nvapi-[A-Za-z0-9_-]+",
        r"Bearer\s+[A-Za-z0-9._~-]+",
        r'(?i)(authorization|password|api[_-]?key)["=: ]+[^,}\s]+',
    )
    result = value
    for pattern in patterns:
        result = re.sub(pattern, "<redacted>", result)
    return result[:800].replace("\n", " ")


def request(
    base: str,
    model: str,
    input_type: str,
) -> tuple[int, int, float, str]:
    payload = json.dumps(
        {
            "model": model,
            "input": ["CVD embedding endpoint validation"],
            "input_type": input_type,
            "truncate": "END",
        }
    ).encode("utf-8")
    request_object = urllib.request.Request(
        f"{base}/v1/embeddings",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "Connection": "close"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request_object, timeout=120) as response:
            body = json.load(response)
            results = body.get("data", [])
            dimensions = len(results[0].get("embedding", [])) if results else 0
            return response.status, dimensions, time.monotonic() - started, ""
    except urllib.error.HTTPError as exc:
        error = redact(exc.read().decode("utf-8", errors="replace"))
        return exc.code, 0, time.monotonic() - started, error


def main() -> int:
    args = parse_args()
    failed = 0
    for base in args.base:
        print(f"Endpoint: {base}")
        print(f"Input type: {args.input_type}")
        endpoint_failed = 0
        for attempt in range(1, args.repeat + 1):
            if attempt > 1 and args.delay:
                time.sleep(args.delay)
            try:
                status, dimensions, elapsed, error = request(
                    base,
                    args.model,
                    args.input_type,
                )
            except Exception as exc:
                status, dimensions, elapsed = 0, 0, 0.0
                print(
                    f"  attempt={attempt} status=transport-error "
                    f"error={type(exc).__name__}"
                )
                endpoint_failed += 1
                continue
            passed = status == 200 and dimensions == args.expected_dimensions
            print(
                f"  attempt={attempt} status={status} dimensions={dimensions} "
                f"elapsed={elapsed:.3f}s passed={passed}"
            )
            if not passed:
                endpoint_failed += 1
                if args.show_error and error:
                    print(f"    NIM error: {error}")
        print(
            f"  summary: passed={args.repeat - endpoint_failed}/{args.repeat} "
            f"failed={endpoint_failed}/{args.repeat}"
        )
        failed += endpoint_failed
    if failed:
        print(f"FAIL: {failed} embedding request(s) failed")
        return 1
    print("PASS: all embedding endpoints and requests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
