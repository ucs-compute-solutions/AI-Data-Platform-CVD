#!/usr/bin/env python3
"""Run the repeatable Document RAG CVD acceptance suite.

The script is designed to be streamed into the existing Insight Engine backend
pod. It reads the already-mounted runtime Secret, suppresses credential values,
uses only synthetic test data, and never deletes test objects.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from dataclasses import dataclass

import requests
import yaml


@dataclass
class Result:
    test_id: str
    name: str
    passed: bool
    detail: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    parser.add_argument(
        "--secret-file",
        default="/etc/insight-engine/secrets/config.yaml",
    )
    parser.add_argument("--ingest-timeout", type=int, default=600)
    parser.add_argument("--retrieve-timeout", type=int, default=300)
    parser.add_argument("--prompt-timeout", type=int, default=900)
    parser.add_argument(
        "--with-rerank",
        action="store_true",
        help="Require reranked retrieval and verify the expected chunk ranks first",
    )
    return parser.parse_args()


def fail(message: str) -> None:
    raise RuntimeError(message)


def require_status(
    response: requests.Response,
    operation: str,
    expected: set[int] | None = None,
) -> None:
    accepted = expected or set(range(200, 300))
    print(f"{operation} HTTP status: {response.status_code}")
    if response.status_code not in accepted:
        fail(f"{operation} returned unexpected HTTP {response.status_code}")


def json_body(response: requests.Response, operation: str) -> object:
    try:
        return response.json()
    except ValueError:
        fail(f"{operation} returned a non-JSON response")


def marker() -> str:
    return str(secrets.randbelow(900_000_000) + 100_000_000)


def pdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def create_text_pdf(lines: list[str]) -> bytes:
    """Create a deterministic one-page, text-only PDF using the PDF base font."""

    commands = ["BT", "/F1 12 Tf", "72 720 Td"]
    for index, line in enumerate(lines):
        if index:
            commands.append("0 -20 Td")
        commands.append(f"({pdf_escape(line)}) Tj")
    commands.append("ET")
    stream = ("\n".join(commands) + "\n").encode("ascii")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"endstream",
    ]

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")

    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


class InsightEngine:
    def __init__(self, args: argparse.Namespace, username: str, password: str):
        self.args = args
        self.username = username
        self.session = requests.Session()
        self.session.headers.update({"Connection": "close"})
        self.retained_collections: list[str] = []
        self.retained_documents: list[str] = []
        self.retained_conversations: list[str] = []

        readiness = self.session.get(
            f"{args.base}/health/readiness",
            timeout=20,
        )
        require_status(readiness, "Backend readiness", {200})

        token_response = self.session.post(
            f"{args.base}/token",
            data={"username": username, "password": password},
            timeout=60,
        )
        require_status(token_response, "Authentication", {200})
        token_body = json_body(token_response, "Authentication")
        if not isinstance(token_body, dict) or not token_body.get("access_token"):
            fail("authentication response contained no access token")
        self.session.headers.update(
            {"Authorization": f"Bearer {token_body['access_token']}"}
        )

    def create_collection(self, name: str) -> None:
        response = self.session.post(
            f"{self.args.base}/api/v1/collections",
            json={"collection_names": [name], "is_public": False},
            timeout=60,
        )
        require_status(response, f"Create collection {name}")
        self.retained_collections.append(name)

    def upload(
        self,
        collection: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> str:
        response = self.session.post(
            f"{self.args.base}/api/v1/documents",
            files={"documents": (filename, content, content_type)},
            data={
                "data": json.dumps(
                    {"collection_name": collection, "is_public": False}
                )
            },
            timeout=120,
        )
        require_status(response, f"Upload document {filename}")
        body = json_body(response, f"Upload document {filename}")
        task_id = body.get("task_id") if isinstance(body, dict) else None
        if not task_id:
            fail(f"upload for {filename} returned no ingestion task ID")
        self.retained_documents.append(f"{collection}/{filename}")
        print(f"Ingestion task for {filename}: {task_id}")
        return str(task_id)

    def wait_for_ingestion(self, task_id: str, filename: str) -> None:
        deadline = time.monotonic() + self.args.ingest_timeout
        previous_state = None
        while time.monotonic() < deadline:
            response = self.session.get(
                f"{self.args.base}/api/v1/status",
                params={"task_id": task_id},
                timeout=60,
            )
            require_status(response, f"Ingestion status {filename}")
            body = json_body(response, f"Ingestion status {filename}")
            state = (
                str(body.get("state", "pending")).lower()
                if isinstance(body, dict)
                else "unknown"
            )
            if state != previous_state:
                print(f"Ingestion state for {filename}: {state}")
                previous_state = state
            if state == "completed":
                return
            if state in {"failed", "skipped"}:
                fail(f"ingestion for {filename} ended in state {state}")
            time.sleep(5)
        fail(
            f"ingestion for {filename} did not complete within "
            f"{self.args.ingest_timeout} seconds"
        )

    def retrieve(self, collection: str, question: str) -> list[dict]:
        response = self.session.post(
            f"{self.args.base}/api/v1/retrieve",
            json={
                "collection_name": collection,
                "prompt": question,
                "number_of_docs": 5,
                "top_k_from_vectorstore": 10,
                "with_rerank": self.args.with_rerank,
                "extra_metadata_fields": [],
            },
            timeout=120,
        )
        require_status(response, f"Retrieve from {collection}", {200})
        body = json_body(response, f"Retrieve from {collection}")
        if not isinstance(body, list):
            fail(f"retrieval from {collection} returned an unexpected structure")
        return [item for item in body if isinstance(item, dict)]

    def wait_for_marker(
        self,
        collection: str,
        question: str,
        expected_marker: str,
    ) -> list[dict]:
        deadline = time.monotonic() + self.args.retrieve_timeout
        while time.monotonic() < deadline:
            chunks = self.retrieve(collection, question)
            if any(expected_marker in str(item.get("content", "")) for item in chunks):
                return chunks
            time.sleep(5)
        fail(
            f"retrieval from {collection} did not return marker "
            f"{expected_marker} within {self.args.retrieve_timeout} seconds"
        )

    def prompt(
        self,
        collection: str,
        question: str,
        expected_marker: str,
        title: str,
    ) -> tuple[float, int]:
        conversation_response = self.session.post(
            f"{self.args.base}/api/v1/conversations",
            json={"title": title, "collection_name": collection},
            timeout=60,
        )
        require_status(conversation_response, f"Create conversation {title}")
        conversation_body = json_body(
            conversation_response, f"Create conversation {title}"
        )
        conversation_id = (
            conversation_body.get("id")
            if isinstance(conversation_body, dict)
            else None
        )
        if conversation_id is None:
            fail(f"conversation {title} returned no ID")
        self.retained_conversations.append(str(conversation_id))

        started = time.monotonic()
        response = self.session.post(
            f"{self.args.base}/api/v1/conversations/{conversation_id}/prompt",
            json={
                "prompt": question,
                "with_rerank": self.args.with_rerank,
                "top_k": 5,
                "top_k_from_vectorstore": 10,
                "extra_metadata_fields": [],
            },
            timeout=self.args.prompt_timeout,
        )
        elapsed = time.monotonic() - started
        require_status(response, f"Grounded prompt {title}", {200})
        body = json_body(response, f"Grounded prompt {title}")
        if not isinstance(body, dict):
            fail(f"grounded prompt {title} returned an unexpected structure")
        answer = str(body.get("content", ""))
        sources = body.get("sources") or []
        if expected_marker not in answer:
            fail(f"grounded prompt {title} did not return the expected marker")
        if not isinstance(sources, list) or not sources:
            fail(f"grounded prompt {title} returned no source chunks")
        print(
            f"Grounded result {title}: marker present; sources={len(sources)}; "
            f"elapsed={elapsed:.2f}s"
        )
        return elapsed, len(sources)


def load_identity(path: str) -> tuple[str, str]:
    with open(path, encoding="utf-8") as stream:
        runtime_secrets = yaml.safe_load(stream)
    mgmt = (runtime_secrets or {}).get("mgmt", {})
    username = mgmt.get("username")
    password = mgmt.get("password")
    if not username or not password:
        fail("runtime manager username or password is unavailable")
    return str(username), str(password)


def record(
    results: list[Result],
    test_id: str,
    name: str,
    function,
) -> object | None:
    print(f"\n{test_id}: {name}")
    try:
        value = function()
    except (RuntimeError, requests.RequestException, OSError, ValueError) as exc:
        results.append(Result(test_id, name, False, str(exc)))
        print(f"FAIL: {type(exc).__name__}: {exc}")
        return None
    results.append(Result(test_id, name, True, "passed"))
    print("PASS")
    return value


def main() -> int:
    args = parse_args()
    run_id = f"{int(time.time())}-{secrets.token_hex(2)}"
    username, password = load_identity(args.secret_file)

    print("Document RAG CVD acceptance")
    print(f"Run ID: {run_id}")
    print(f"Backend: {args.base}")
    print(f"Identity: {username}")
    print("Credential source: mounted runtime Secret (values suppressed)")
    print(f"Reranking: {'required' if args.with_rerank else 'disabled'}")
    print("Cleanup policy: retain all synthetic test objects")

    try:
        client = InsightEngine(args, username, password)
    except (RuntimeError, requests.RequestException, OSError, ValueError) as exc:
        print(f"FAIL: initialization: {type(exc).__name__}: {exc}")
        return 1

    results: list[Result] = []
    text_collection = f"cvd-txt-{run_id}"
    pdf_collection = f"cvd-pdf-{run_id}"
    multi_collection = f"cvd-multi-{run_id}"

    text_marker = marker()
    pdf_marker = marker()
    text_filename = f"cvd-text-{run_id}.txt"
    pdf_filename = f"cvd-text-pdf-{run_id}.pdf"
    text_question = (
        "Using only the retrieved document, what is the CVD text validation "
        "marker? Return only the numeric marker."
    )
    pdf_question = (
        "Using only the retrieved PDF, what is the CVD PDF validation marker? "
        "Return only the numeric marker."
    )

    def test_text() -> None:
        client.create_collection(text_collection)
        document = (
            "Cisco Validated Design Document RAG text acceptance.\n"
            f"The CVD text validation marker is {text_marker}.\n"
            "Return this marker when asked for the CVD text validation marker.\n"
        ).encode("utf-8")
        task = client.upload(
            text_collection,
            text_filename,
            document,
            "text/plain",
        )
        client.wait_for_ingestion(task, text_filename)
        chunks = client.wait_for_marker(text_collection, text_question, text_marker)
        if args.with_rerank and text_marker not in str(chunks[0].get("content", "")):
            fail("reranker did not place the expected text chunk first")
        client.prompt(
            text_collection,
            text_question,
            text_marker,
            f"CVD TXT {run_id}",
        )

    record(results, "CVD-RAG-03", "TXT complete Document RAG flow", test_text)

    def test_pdf() -> None:
        client.create_collection(pdf_collection)
        pdf = create_text_pdf(
            [
                "Cisco Validated Design Document RAG PDF acceptance.",
                f"The CVD PDF validation marker is {pdf_marker}.",
                "Return this marker when asked for the CVD PDF validation marker.",
            ]
        )
        task = client.upload(
            pdf_collection,
            pdf_filename,
            pdf,
            "application/pdf",
        )
        client.wait_for_ingestion(task, pdf_filename)
        chunks = client.wait_for_marker(pdf_collection, pdf_question, pdf_marker)
        if args.with_rerank and pdf_marker not in str(chunks[0].get("content", "")):
            fail("reranker did not place the expected PDF chunk first")
        client.prompt(
            pdf_collection,
            pdf_question,
            pdf_marker,
            f"CVD PDF {run_id}",
        )

    record(results, "CVD-RAG-04", "Text-based PDF complete Document RAG flow", test_pdf)

    topics = [
        ("Aurora networking", marker()),
        ("Borealis storage", marker()),
        ("Cygnus GPU", marker()),
        ("Draco telemetry", marker()),
        ("Equinox security", marker()),
    ]

    def test_multidoc() -> None:
        client.create_collection(multi_collection)
        for index, (topic, expected) in enumerate(topics, start=1):
            filename = f"cvd-multi-{index}-{run_id}.txt"
            document = (
                f"Cisco Validated Design synthetic topic: {topic}.\n"
                f"The validation marker for {topic} is {expected}.\n"
                f"Only use {expected} when asked specifically about {topic}.\n"
            ).encode("utf-8")
            task = client.upload(
                multi_collection,
                filename,
                document,
                "text/plain",
            )
            client.wait_for_ingestion(task, filename)

        for index, (topic, expected) in enumerate(topics, start=1):
            question = (
                f"Using only the retrieved documents, what is the validation "
                f"marker for {topic}? Return only the numeric marker."
            )
            chunks = client.wait_for_marker(multi_collection, question, expected)
            if args.with_rerank and expected not in str(chunks[0].get("content", "")):
                fail(f"reranker did not rank the {topic} document first")
            client.prompt(
                multi_collection,
                question,
                expected,
                f"CVD multi {index} {run_id}",
            )

    record(
        results,
        "CVD-RAG-05/06/08",
        "Multi-document retrieval, reranking, grounding and sources",
        test_multidoc,
    )

    def test_isolation() -> None:
        if text_collection not in client.retained_collections:
            fail("TXT collection was not created; collection isolation cannot run")
        if pdf_collection not in client.retained_collections:
            fail("PDF collection was not created; collection isolation cannot run")
        chunks = client.retrieve(pdf_collection, text_question)
        content = "\n".join(str(chunk.get("content", "")) for chunk in chunks)
        if text_marker in content:
            fail("TXT marker was returned while querying the PDF collection")

    record(results, "CVD-RAG-07", "Private collection boundary", test_isolation)

    def test_invalid_authentication() -> None:
        session = requests.Session()
        session.headers.update(
            {
                "Authorization": "Bearer cvd-intentionally-invalid-token",
                "Connection": "close",
            }
        )
        response = session.post(
            f"{args.base}/api/v1/retrieve",
            json={
                "collection_name": text_collection,
                "prompt": text_question,
                "number_of_docs": 1,
                "top_k_from_vectorstore": 1,
                "with_rerank": False,
                "extra_metadata_fields": [],
            },
            timeout=60,
        )
        require_status(response, "Invalid-token request", {401})

    record(results, "CVD-RAG-09", "Invalid bearer token is rejected", test_invalid_authentication)

    print("\nCVD acceptance summary")
    for result in results:
        state = "PASS" if result.passed else "FAIL"
        print(f"{result.test_id}: {state} - {result.name}")

    print("\nRetained synthetic test objects")
    print("Collections:")
    for name in client.retained_collections:
        print(f"  {name}")
    print("Documents:")
    for name in client.retained_documents:
        print(f"  {name}")
    print("Conversations:")
    for conversation_id in client.retained_conversations:
        print(f"  {conversation_id}")
    print("Cleanup: not performed; separate approval is required")

    failed = [result for result in results if not result.passed]
    if failed:
        print(f"FAIL: {len(failed)} CVD test group(s) failed")
        return 1
    print("PASS: Document RAG CVD functional acceptance")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("FAIL: interrupted")
        sys.exit(130)
