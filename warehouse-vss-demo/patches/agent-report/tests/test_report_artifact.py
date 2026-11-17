"""Offline public-artifact acceptance tests; no model or network request."""

import importlib.util
from pathlib import Path
import ssl
import unittest
import urllib.error
import urllib.request


SCRIPT = Path(__file__).resolve().parents[1] / "validate-report-artifact.py"
SPEC = importlib.util.spec_from_file_location("report_artifact_acceptance", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

BASE = "https://warehouse.example/static/"
URL = BASE + "report.md"
INCIDENT = "0fe88cf6816b424dd09fc76eeea63fe6919c54e6"
REPORT = (
    "# Warehouse Incident Report\n"
    f"Incident ID: {INCIDENT}\nSensor ID: Camera\n"
    "A person walks along a warehouse aisle. "
    + "No cause or safety conclusion is established from this clip. " * 5
).encode()


class FakeResponse:
    def __init__(self, body=REPORT, *, status=200, url=URL, content_type="text/markdown"):
        self.body = body
        self.status = status
        self.url = url
        self.headers = {"Content-Type": content_type}
        self.read_limit = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size):
        self.read_limit = size
        return self.body[:size]

    def geturl(self):
        return self.url


class FakeFactory:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, origin, ca_bundle):
        parent = self

        class FakeOpener:
            def open(self, request, timeout):
                parent.requests.append((request, timeout, origin, ca_bundle))
                response = parent.responses.pop(0)
                if isinstance(response, BaseException):
                    raise response
                return response

        return FakeOpener()


class ReportArtifactAcceptanceTests(unittest.TestCase):
    def check(self, *responses, **kwargs):
        self.factory = FakeFactory(*responses)
        args = dict(artifact_url=URL, expected_base_url=BASE, incident_id=INCIDENT,
                    sensor="Camera", expected_fact="aisle", opener_factory=self.factory)
        args.update(kwargs)
        return MODULE.check_artifact(**args)

    def test_public_url_not_rewritten_and_only_get_is_used(self):
        response = FakeResponse()
        result = self.check(response)
        request, timeout, _origin, _ca = self.factory.requests[0]
        self.assertEqual(request.full_url, URL)
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(timeout, 30)
        self.assertEqual(response.read_limit, MODULE.MAX_REPORT_BYTES + 1)
        self.assertEqual(result[0]["status"], 200)
        self.assertEqual(result[0]["bytes"], len(REPORT))
        self.assertEqual(len(result[0]["sha256"]), 64)

    def test_repeated_artifact_reads_have_consistent_digest(self):
        result = self.check(FakeResponse(), FakeResponse(), samples=2)
        self.assertEqual(result[0]["sha256"], result[1]["sha256"])
        self.assertEqual(len(self.factory.requests), 2)

    def test_changed_artifact_fails(self):
        with self.assertRaisesRegex(MODULE.AcceptanceError, "changed between samples"):
            self.check(FakeResponse(), FakeResponse(REPORT + b"\nChanged."), samples=2)

    def test_404_fails_without_reading_error_body(self):
        error = urllib.error.HTTPError(URL, 404, "Not Found", {}, None)
        with self.assertRaisesRegex(MODULE.AcceptanceError, "HTTP 404"):
            self.check(error)

    def test_non_200_response_fails(self):
        with self.assertRaisesRegex(MODULE.AcceptanceError, "HTTP 206"):
            self.check(FakeResponse(status=206))

    def test_html_content_type_fails(self):
        with self.assertRaisesRegex(MODULE.AcceptanceError, "HTML"):
            self.check(FakeResponse(content_type="text/html"))

    def test_html_mislabeled_as_markdown_fails(self):
        with self.assertRaisesRegex(MODULE.AcceptanceError, "HTML"):
            self.check(FakeResponse(b"<!DOCTYPE html><html><body>Not a report</body></html>"))

    def test_report_must_contain_exact_incident_sensor_and_scene(self):
        for body, expected in (
            (REPORT.replace(INCIDENT.encode(), b"different-incident"), "exact incident"),
            (REPORT.replace(b"Camera", b"Sensor_99"), "sensor"),
            (REPORT.replace(b"aisle", b"corridor"), "scene fact"),
        ):
            with self.subTest(expected=expected), self.assertRaisesRegex(MODULE.AcceptanceError, expected):
                self.check(FakeResponse(body))

    def test_incident_or_camera_prefix_cannot_match_a_different_identity(self):
        for body, expected in (
            (REPORT.replace(INCIDENT.encode(), (INCIDENT + "0").encode()), "exact incident"),
            (REPORT.replace(b"Camera", b"Camera_01"), "sensor"),
        ):
            with self.subTest(expected=expected), self.assertRaisesRegex(MODULE.AcceptanceError, expected):
                self.check(FakeResponse(body))

    def test_short_or_wrong_title_and_placeholder_fail(self):
        for body in (b"short", REPORT.replace(b"Warehouse Incident Report", b"Login page"), REPORT + b" placeholder"):
            with self.subTest(body_size=len(body)), self.assertRaises(MODULE.AcceptanceError):
                self.check(FakeResponse(body))

    def test_binary_and_oversize_fail(self):
        for body in (b"\xff" + REPORT, REPORT + b"x" * MODULE.MAX_REPORT_BYTES):
            with self.subTest(body_size=len(body)), self.assertRaises(MODULE.AcceptanceError):
                self.check(FakeResponse(body))

    def test_unsafe_url_forms_fail_before_request(self):
        paths = (
            "http://warehouse.example/static/report.md",
            "https://user:password@warehouse.example/static/report.md",
            URL + "?token=value", URL + "#fragment", URL + "?", URL + "#",
            "https://other.example/static/report.md",
            "https://warehouse.example/statics/report.md",
            BASE + "../report.md", BASE + "%2e%2e/report.md",
            BASE + "%252e%252e/report.md", BASE + "sub%2freport.md",
            BASE + "sub%255creport.md", BASE + "sub\\report.md",
            BASE + "bad%00name.md", BASE + "bad%zz.md", BASE + "%ff.md", BASE,
        )
        for value in paths:
            with self.subTest(url=value), self.assertRaises(MODULE.AcceptanceError):
                self.check(FakeResponse(), artifact_url=value)
            self.assertEqual(self.factory.requests, [])

    def test_unsafe_base_url_is_rejected(self):
        with self.assertRaises(MODULE.AcceptanceError):
            self.check(FakeResponse(), expected_base_url="http://warehouse.example/static/")

    def test_final_response_url_is_revalidated(self):
        with self.assertRaisesRegex(MODULE.AcceptanceError, "outside"):
            self.check(FakeResponse(url="https://other.example/static/report.md"))

    def test_redirect_handler_accepts_same_tree_and_rejects_foreign_origin(self):
        handler = MODULE.ConstrainedRedirectHandler(MODULE.ReportOrigin(BASE))
        request = urllib.request.Request(URL)
        allowed = handler.redirect_request(request, None, 302, "Found", {}, BASE + "new.md")
        self.assertEqual(allowed.full_url, BASE + "new.md")
        for value in ("https://foreign.example/static/r.md", "https://warehouse.example/private/r.md", BASE + "../r.md"):
            with self.subTest(url=value), self.assertRaises(MODULE.AcceptanceError):
                handler.redirect_request(request, None, 302, "Found", {}, value)

    def test_redirect_count_is_bounded(self):
        handler = MODULE.ConstrainedRedirectHandler(MODULE.ReportOrigin(BASE))
        request = urllib.request.Request(URL)
        for _ in range(MODULE.MAX_REDIRECTS):
            handler.redirect_request(request, None, 302, "Found", {}, URL)
        with self.assertRaisesRegex(MODULE.AcceptanceError, "redirect bound"):
            handler.redirect_request(request, None, 302, "Found", {}, URL)

    def test_sample_count_bounds(self):
        for samples in (0, 21):
            with self.subTest(samples=samples), self.assertRaisesRegex(MODULE.AcceptanceError, "Sample count"):
                self.check(FakeResponse(), samples=samples)

    def test_tls_context_verification_remains_enabled(self):
        opener = MODULE.build_opener(MODULE.ReportOrigin(BASE))
        https = next(handler for handler in opener.handlers if isinstance(handler, urllib.request.HTTPSHandler))
        self.assertTrue(https._context.check_hostname)
        self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)


if __name__ == "__main__":
    unittest.main()
