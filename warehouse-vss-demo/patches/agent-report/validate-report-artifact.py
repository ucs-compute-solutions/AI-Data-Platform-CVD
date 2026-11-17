#!/usr/bin/env python3
"""Read-only acceptance of an existing Warehouse report at its public HTTPS URL.

This command never invokes an Agent or model. It checks the supplied report
through the real HTTPS origin, including bounded same-origin redirects, and
prints only status, byte count, digest, and acceptance metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request


MAX_REPORT_BYTES = 2_000_000
MAX_REDIRECTS = 5
REQUEST_TIMEOUT = 30


class AcceptanceError(ValueError):
    """A bounded, non-sensitive acceptance failure."""


def checked_url(value: str) -> tuple[urllib.parse.SplitResult, str]:
    """Validate an HTTPS URL without normalizing away an unsafe path."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise AcceptanceError("URL is empty or exceeds the length bound")
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise AcceptanceError("URL contains whitespace or control characters")
    if "\\" in value or "?" in value or "#" in value:
        raise AcceptanceError("URL contains a backslash, query, or fragment")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise AcceptanceError("URL could not be parsed") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise AcceptanceError("URL must be HTTPS without credentials, query, or fragment")
    path = parsed.path or "/"
    if not path.startswith("/") or re.search(r"%(?![0-9a-fA-F]{2})", path):
        raise AcceptanceError("URL path has invalid encoding")
    # Decode repeatedly to reject double-encoded traversal and separators as
    # well as their single-encoded forms. A public report path needs neither.
    decoded = path
    for _ in range(8):
        if re.search(r"%(?:2f|5c)", decoded, flags=re.IGNORECASE):
            raise AcceptanceError("URL path contains an encoded separator")
        if "\\" in decoded or any(ord(char) <= 32 or ord(char) == 127 for char in decoded):
            raise AcceptanceError("URL path contains unsafe characters")
        if any(segment in (".", "..") for segment in decoded.split("/")):
            raise AcceptanceError("URL path contains traversal")
        try:
            next_value = urllib.parse.unquote(decoded, encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            raise AcceptanceError("URL path is not valid UTF-8") from None
        if next_value == decoded:
            break
        decoded = next_value
    else:
        raise AcceptanceError("URL path has excessive encoding")
    return parsed, decoded


class ReportOrigin:
    """Restrict both the first request and every redirect to one report tree."""

    def __init__(self, base_url: str):
        parsed, path = checked_url(base_url)
        self.origin = (parsed.scheme, parsed.hostname.lower(), parsed.port or 443)
        self.prefix = path.rstrip("/") + "/"

    def validate(self, url: str) -> str:
        parsed, path = checked_url(url)
        origin = (parsed.scheme, parsed.hostname.lower(), parsed.port or 443)
        if origin != self.origin or not path.startswith(self.prefix):
            raise AcceptanceError("Report URL is outside the expected origin or path prefix")
        if path == self.prefix:
            raise AcceptanceError("Report URL names a directory rather than an artifact")
        return url


class ConstrainedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin: ReportOrigin):
        super().__init__()
        self.origin = origin
        self.redirects = 0

    def redirect_request(self, request, response, code, message, headers, new_url):
        self.redirects += 1
        if self.redirects > MAX_REDIRECTS:
            raise AcceptanceError("Report exceeded the redirect bound")
        self.origin.validate(new_url)
        return super().redirect_request(request, response, code, message, headers, new_url)


def build_opener(origin: ReportOrigin, ca_bundle: str | None = None):
    # An issuer bundle adds site trust without disabling either certificate or
    # hostname verification. Proxy environment variables are not inherited.
    context = ssl.create_default_context()
    if ca_bundle:
        context.load_verify_locations(cafile=ca_bundle)
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        ConstrainedRedirectHandler(origin),
    )


def validate_content(raw: bytes, content_type: str, *, incident_id: str, sensor: str, fact: str) -> None:
    if len(raw) > MAX_REPORT_BYTES:
        raise AcceptanceError("Report exceeded the bounded read limit")
    if "html" in content_type.casefold():
        raise AcceptanceError("Report returned HTML rather than a Markdown artifact")
    try:
        report = raw.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        raise AcceptanceError("Report is not valid UTF-8 text") from None
    if re.search(r"<!doctype\s+html|<html\b|<head\b|<body\b", report[:2048], re.IGNORECASE):
        raise AcceptanceError("Report body is an HTML page")
    if len(report) < 200 or "Warehouse Incident Report" not in report:
        raise AcceptanceError("Artifact is missing substantive Warehouse report text")
    # An artifact needs its own exact incident ID: a correct chat wrapper must
    # not make an unrelated file pass. Sensor/fact matching follows the existing
    # report-chat acceptance semantics.
    if not re.search(r"(?<![A-Za-z0-9._:-])" + re.escape(incident_id) + r"(?![A-Za-z0-9._:-])", report):
        raise AcceptanceError("Report omitted the exact incident ID")
    if not re.search(r"(?<![A-Za-z0-9._:-])" + re.escape(sensor) + r"(?![A-Za-z0-9._:-])", report, re.IGNORECASE):
        raise AcceptanceError("Report omitted the expected sensor ID")
    if fact.casefold() not in report.casefold():
        raise AcceptanceError("Report omitted the expected scene fact")
    if re.search(r"\bplaceholder\b|\[\s*(?:insert|fill in|tbd)\b|\blorem ipsum\b|\breplace me\b", report, re.IGNORECASE):
        raise AcceptanceError("Report contains a literal placeholder")


def check_artifact(
    *, artifact_url: str, expected_base_url: str, incident_id: str,
    sensor: str, expected_fact: str, samples: int = 1,
    ca_bundle: str | None = None, opener_factory=build_opener,
) -> list[dict[str, object]]:
    if not 1 <= samples <= 20:
        raise AcceptanceError("Sample count must be 1–20")
    origin = ReportOrigin(expected_base_url)
    origin.validate(artifact_url)
    results = []
    expected_digest = None
    for index in range(samples):
        # A new opener per sample also resets the bounded redirect counter.
        opener = opener_factory(origin, ca_bundle)
        request = urllib.request.Request(
            artifact_url, method="GET",
            headers={"Accept": "text/markdown,text/plain;q=0.9,application/octet-stream;q=0.5"},
        )
        try:
            with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
                status = response.status
                origin.validate(response.geturl())
                raw = response.read(MAX_REPORT_BYTES + 1)
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            raise AcceptanceError(f"Report returned HTTP {exc.code} at sample {index + 1}") from None
        except urllib.error.URLError:
            raise AcceptanceError(f"HTTPS report request failed at sample {index + 1}") from None
        except (TimeoutError, OSError):
            raise AcceptanceError(f"Report transport failed at sample {index + 1}") from None
        if status != 200:
            raise AcceptanceError(f"Report returned HTTP {status} at sample {index + 1}")
        validate_content(raw, content_type, incident_id=incident_id, sensor=sensor, fact=expected_fact)
        digest = hashlib.sha256(raw).hexdigest()
        if expected_digest is not None and digest != expected_digest:
            raise AcceptanceError(f"Report content changed between samples at sample {index + 1}")
        expected_digest = digest
        results.append({"sample": index + 1, "status": status, "bytes": len(raw), "sha256": digest})
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-url", required=True)
    parser.add_argument("--expected-base-url", required=True)
    parser.add_argument("--incident-id", required=True)
    parser.add_argument("--sensor", required=True)
    parser.add_argument("--expected-fact", required=True)
    parser.add_argument("--ca-bundle")
    parser.add_argument("--samples", type=int, default=1)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", args.incident_id):
        parser.error("--incident-id must be an exact safe identifier")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,127}", args.sensor):
        parser.error("--sensor must be an exact safe identifier")
    if not args.expected_fact.strip() or len(args.expected_fact) > 100 or any(ord(c) < 32 for c in args.expected_fact):
        parser.error("--expected-fact must be a short nonempty scene fact")
    if not 1 <= args.samples <= 20:
        parser.error("--samples must be 1–20")
    try:
        results = check_artifact(
            artifact_url=args.artifact_url, expected_base_url=args.expected_base_url,
            incident_id=args.incident_id, sensor=args.sensor, expected_fact=args.expected_fact,
            samples=args.samples, ca_bundle=args.ca_bundle,
        )
    except (AcceptanceError, ValueError, OSError):
        # Only these authored messages are printed; exception text from TLS,
        # certificates, arbitrary URLs, or remote response bodies is not exposed.
        exc = sys.exc_info()[1]
        message = str(exc) if isinstance(exc, AcceptanceError) else "Report trust or input validation failed"
        print(f"FAIL: {message}", file=sys.stderr)
        return 1
    for result in results:
        print(json.dumps(result, sort_keys=True))
    print(f"PASS: {len(results)} HTTPS artifact read(s); exact incident/sensor/scene checks; stable SHA-256; no inference")
    return 0


if __name__ == "__main__":
    sys.exit(main())
