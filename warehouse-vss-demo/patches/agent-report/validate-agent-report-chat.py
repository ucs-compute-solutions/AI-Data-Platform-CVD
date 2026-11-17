#!/usr/bin/env python3
"""Bounded Agent report-chat acceptance against an existing incident.

Runs inside the Warehouse Agent Pod and prints no generated report text.
The request invokes report generation in the Agent's in-memory object store;
it does not upload, delete, or alter Kubernetes resources.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


MAX_RESPONSE_BYTES = 2_000_000
PLACEHOLDER_PATTERNS = (
    re.compile(r"\bplaceholder\b", re.IGNORECASE),
    re.compile(r"\[\s*(?:insert|fill in|tbd)\b", re.IGNORECASE),
    re.compile(r"\b(?:lorem ipsum|replace me)\b", re.IGNORECASE),
)


def answer_text(body: object) -> str:
    if not isinstance(body, dict):
        raise ValueError("Agent response is not a JSON object")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("Agent response has no choices")
    choice = choices[0]
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise ValueError("Agent response has no assistant message")
    content = choice["message"].get("content")
    if isinstance(content, list):
        content = "\n".join(
            block.get("text", "") for block in content if isinstance(block, dict)
        )
    if not isinstance(content, str):
        raise ValueError("Agent assistant content is not text")
    return content.strip()


def visible_answer(text: str) -> str:
    """Exclude internal Agent reasoning/tool traces from user-answer checks."""
    if "<agent-think>" in text and "</agent-think>" not in text:
        raise ValueError("Agent reasoning trace has no closing delimiter")
    visible = re.sub(r"<agent-think>.*?</agent-think>", "", text, flags=re.DOTALL)
    visible = re.sub(r"<think>.*?</think>", "", visible, flags=re.DOTALL)
    return visible.strip()


def validate_answer(
    text: str,
    *,
    incident_id: str,
    sensor: str,
    fact: str,
    category: str | None = None,
    verdict: str | None = None,
) -> None:
    if len(text) < 80:
        raise ValueError("Agent answer is empty or too short for a grounded report")
    for expected, label in (
        (incident_id, "incident ID"),
        (sensor, "sensor ID"),
        (fact, "grounded fact"),
    ):
        if expected.casefold() not in text.casefold():
            raise ValueError(f"Agent answer omitted the expected {label}")
    if "[Markdown Report](" not in text:
        raise ValueError("Agent answer has no Markdown report artifact link")
    if category is not None and verdict is not None:
        label = f"**Alerts index classification:** {category} (verdict: {verdict})."
        if label not in text:
            raise ValueError("Agent answer omitted the exact enriched alert classification")
        if "**Model-generated video report:**" not in text or "They are not reconciled here" not in text:
            raise ValueError("Agent answer omitted the separate report assessment or disagreement note")
    if any(pattern.search(text) for pattern in PLACEHOLDER_PATTERNS):
        raise ValueError("Agent answer contains a literal placeholder")


def safe_excerpt(text: str, limit: int = 650) -> str:
    """Bound an explicitly requested diagnostic excerpt and mask common secrets."""
    masked = re.sub(r"https?://[^\s)]+", "[URL]", text)
    masked = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [REDACTED]", masked)
    masked = re.sub(
        r"(?i)\b(password|api[_-]?key|token|secret)\b\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        masked,
    )
    masked = re.sub(r"\b[A-Za-z0-9+/]{48,}={0,2}\b", "[LONG_VALUE]", masked)
    return " ".join(masked[:limit].split())


def fetch_markdown_report(text: str, opener: urllib.request.OpenerDirector, timeout: int) -> str:
    """Read the just-created report via this same Agent's local static server."""
    match = re.search(r"\[Markdown Report\]\((https://[^\s)]+)\)", text)
    if not match:
        raise ValueError("Agent answer has no HTTPS Markdown report link")
    report_url = urllib.parse.urlsplit(match.group(1))
    configured_base = os.environ.get("VSS_AGENT_REPORTS_BASE_URL", "")
    base = urllib.parse.urlsplit(configured_base)
    if (
        not base.hostname
        or report_url.scheme != "https"
        or report_url.netloc != base.netloc
        or not report_url.path.startswith(base.path.rstrip("/") + "/")
        or report_url.username
        or report_url.password
        or report_url.query
        or report_url.fragment
    ):
        raise ValueError("Markdown report URL differs from the configured Agent static origin")
    local_url = "http://127.0.0.1:8000" + report_url.path
    with opener.open(
        urllib.request.Request(local_url, method="GET"), timeout=timeout
    ) as response:
        if response.status != 200:
            raise ValueError(f"Markdown report returned HTTP {response.status}")
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Markdown report exceeded the bounded read limit")
    return raw.decode("utf-8").strip()


def validate_report(report: str, *, sensor: str, fact: str) -> None:
    if len(report) < 200 or "Warehouse Incident Report" not in report:
        raise ValueError("Markdown artifact is missing substantive report text")
    if sensor.casefold() not in report.casefold():
        raise ValueError("Markdown artifact omitted the expected sensor ID")
    if fact.casefold() not in report.casefold():
        raise ValueError("Markdown artifact omitted the expected scene fact")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incident-id", required=True)
    parser.add_argument("--sensor", required=True)
    parser.add_argument("--expected-fact", required=True)
    parser.add_argument("--expected-category")
    parser.add_argument("--expected-verdict")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Print only bounded term-presence and length diagnostics, never answer text",
    )
    parser.add_argument(
        "--diagnose-excerpt",
        action="store_true",
        help="Print one 650-character redacted excerpt for incident-specific troubleshooting",
    )
    parser.add_argument(
        "--diagnose-tail",
        action="store_true",
        help="Print a redacted tail excerpt to distinguish reasoning trace from final answer",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", args.incident_id):
        parser.error("--incident-id must be an exact, safe identifier")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,127}", args.sensor):
        parser.error("--sensor must be an exact, safe identifier")
    if not args.expected_fact.strip() or len(args.expected_fact) > 100:
        parser.error("--expected-fact must be a short known report fact")
    if (args.expected_category is None) != (args.expected_verdict is None):
        parser.error("--expected-category and --expected-verdict must be supplied together")
    for value in (args.expected_category, args.expected_verdict):
        if value is not None and (not value.strip() or len(value) > 100):
            parser.error("expected classification values must be short nonempty strings")
    if not 30 <= args.timeout <= 900:
        parser.error("--timeout must be 30–900 seconds")

    prompt = (
        f"Generate a detailed report for incident {args.incident_id} from "
        f"{args.sensor}. In your answer, include the incident ID, sensor ID, "
        "and only findings supported by the report. State uncertainty; "
        "do not use placeholders."
    )
    payload = json.dumps(
        {"messages": [{"role": "user", "content": prompt}], "stream": False},
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        "http://127.0.0.1:8000/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=args.timeout) as response:
            status = response.status
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Agent returned HTTP {exc.code}") from None
    if status != 200:
        raise ValueError(f"Agent returned HTTP {status}")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Agent response exceeded the bounded read limit")
    body = json.loads(raw)
    full_text = answer_text(body)
    text = visible_answer(full_text)
    if args.diagnose:
        observed_terms = (
            "worker",
            "forklift",
            "near miss",
            "violation",
            "warehouse",
            "report",
            "video",
            "uncertain",
        )
        flags = ",".join(
            f"{term.replace(' ', '_')}={term in text.casefold()}"
            for term in observed_terms
        )
        print(
            f"DIAG: raw_chars={len(full_text)}; visible_chars={len(text)}; "
            f"incident_id={args.incident_id in text}; sensor={args.sensor in text}; "
            f"links={text.count('](')}; {flags}"
        )
    if args.diagnose_excerpt:
        print(f"DIAG excerpt: {safe_excerpt(text)}")
    if args.diagnose_tail:
        print(f"DIAG tail: {safe_excerpt(text[-1000:])}")
    report = fetch_markdown_report(text, opener, args.timeout)
    if args.diagnose:
        report_terms = ("worker", "person", "aisle", "forklift", "box", "pallet", "warehouse", "unknown")
        flags = ",".join(f"{term}={term in report.casefold()}" for term in report_terms)
        print(f"DIAG report: chars={len(report)}; {flags}")
    validate_report(report, sensor=args.sensor, fact=args.expected_fact)
    validate_answer(
        text,
        incident_id=args.incident_id,
        sensor=args.sensor,
        fact=args.expected_fact,
        category=args.expected_category,
        verdict=args.expected_verdict,
    )
    print(
        f"PASS: Agent report chat HTTP 200; incident={args.incident_id}; "
        f"sensor={args.sensor}; answer_chars={len(text)}; "
        f"report_chars={len(report)}; shared scene fact present; no placeholder"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
