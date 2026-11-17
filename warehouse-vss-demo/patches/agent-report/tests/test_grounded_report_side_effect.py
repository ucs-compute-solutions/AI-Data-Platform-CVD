"""Offline checks for the Warehouse-only deterministic report evidence block."""

import ast
import json
from pathlib import Path
from typing import Any
import unittest
import urllib.request


PATCH = Path(__file__).resolve().parents[1] / "0002-append-grounded-report-evidence.patch"
INCIDENT = "d515103ab0428e1db86e1778950b73d0011e8bb6"


def load_helpers() -> dict[str, Any]:
    lines: list[str] = []
    inside = False
    for line in PATCH.read_text(encoding="utf-8").splitlines():
        if line.startswith("+_ALERT_INDEX_URL"):
            inside = True
        if inside:
            if line.startswith("+") and not line.startswith("+++"):
                lines.append(line[1:])
            else:
                break
    if not lines:
        raise ValueError("grounded report helper is missing from the patch")
    tree = ast.parse("\n".join(lines))
    namespace: dict[str, Any] = {"Any": Any, "json": json, "urllib": urllib}
    exec(compile(tree, "report_agent_patch2.py", "exec"), namespace)
    return namespace


class FakeResponse:
    status = 200

    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size: int):
        return self.body[:size]


class FakeOpener:
    def __init__(self, record: dict[str, Any]):
        self.record = record
        self.last_request = None
        self.last_timeout = None

    def open(self, request, *, timeout: int):
        self.last_request = request
        self.last_timeout = timeout
        body = json.dumps({"hits": {"hits": [{"_source": self.record}]}}).encode()
        return FakeResponse(body)


class GroundedReportSideEffectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = load_helpers()

    def test_exact_enriched_classification(self):
        opener = FakeOpener(
            {"Id": INCIDENT, "sensorId": "Camera_01", "category": "Near Miss Violation", "info": {"verdict": "confirmed"}}
        )
        classify = self.helpers["_enriched_alert_classification"]
        self.assertEqual(classify(INCIDENT, "Camera_01", opener), ("Near Miss Violation", "confirmed"))
        self.assertEqual(opener.last_timeout, 3)
        self.assertEqual(opener.last_request.get_method(), "POST")
        payload = json.loads(opener.last_request.data)
        self.assertEqual(payload["query"]["term"]["Id.keyword"], INCIDENT)
        self.assertEqual(payload["size"], 1)

    def test_mismatched_id_or_sensor_cannot_be_presented(self):
        classify = self.helpers["_enriched_alert_classification"]
        wrong_id = FakeOpener({"Id": "different", "sensorId": "Camera_01", "category": "Near Miss Violation", "info": {"verdict": "confirmed"}})
        wrong_sensor = FakeOpener({"Id": INCIDENT, "sensorId": "Camera_03", "category": "Near Miss Violation", "info": {"verdict": "confirmed"}})
        self.assertIsNone(classify(INCIDENT, "Camera_01", wrong_id))
        self.assertIsNone(classify(INCIDENT, "Camera_01", wrong_sensor))

    def test_report_and_classification_are_labeled_separately(self):
        render = self.helpers["_grounded_report_side_effect"]
        report = "# Warehouse Incident Report\nA person is visible in an aisle; no incident is established."
        result = render(INCIDENT, "Camera_01", report, ("Near Miss Violation", "confirmed"))
        self.assertIn(INCIDENT, result)
        self.assertIn("Camera_01", result)
        self.assertIn("Near Miss Violation (verdict: confirmed)", result)
        self.assertIn(report, result)
        self.assertIn("separate model outputs", result)
        self.assertIn("review the video", result)
        self.assertNotIn("Verified near miss", result)

    def test_missing_classification_is_not_invented(self):
        render = self.helpers["_grounded_report_side_effect"]
        result = render(INCIDENT, "Camera_01", "A worker walks through the aisle.", None)
        self.assertIn("classification:** unavailable", result)
        self.assertNotIn("Near Miss Violation", result)

    def test_live_callsite_appends_evidence_not_only_context(self):
        patch = PATCH.read_text(encoding="utf-8")
        self.assertIn('side_effects["report_evidence"]', patch)
        self.assertIn("await asyncio.to_thread(", patch)
        self.assertIn("+ report_text,", patch)


if __name__ == "__main__":
    unittest.main()
