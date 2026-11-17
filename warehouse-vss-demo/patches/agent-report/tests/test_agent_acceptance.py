"""Local tests for bounded Agent chat acceptance, without an API request."""

import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "validate-agent-report-chat.py"
SPEC = importlib.util.spec_from_file_location("agent_acceptance", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AgentAcceptanceTests(unittest.TestCase):
    def test_openai_response_extracts_text(self) -> None:
        body = {"choices": [{"message": {"content": "Grounded response"}}]}
        self.assertEqual(MODULE.answer_text(body), "Grounded response")

    def test_visible_answer_excludes_internal_trace(self) -> None:
        full = (
            "<agent-think><agent-think-step title=\"Tool\">"
            "Near Miss Camera_01 abcdefgh</agent-think-step></agent-think>"
            "Final report: person walking in a warehouse aisle."
        )
        visible = MODULE.visible_answer(full)
        self.assertEqual(visible, "Final report: person walking in a warehouse aisle.")
        self.assertNotIn("Near Miss", visible)

    def test_grounded_answer_passes(self) -> None:
        text = (
            "Incident d515103ab0428e1db86e1778950b73d0011e8bb6 from Camera_01 "
            "was classified as Near Miss Violation. The clip visibly shows a worker "
            "near a forklift; the cause cannot be established from this clip alone. "
            "[Markdown Report](https://example.invalid/static/report.md)"
        )
        MODULE.validate_answer(
            text,
            incident_id="d515103ab0428e1db86e1778950b73d0011e8bb6",
            sensor="Camera_01",
            fact="Near Miss",
        )

    def test_literal_placeholder_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "placeholder"):
            MODULE.validate_answer(
                "Incident abcdefgh on Camera_01. Near Miss. "
                "This is a placeholder, with more content coming later in the report. "
                "[Markdown Report](https://example.invalid/static/report.md)",
                incident_id="abcdefgh",
                sensor="Camera_01",
                fact="Near Miss",
            )

    def test_missing_fact_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "grounded fact"):
            MODULE.validate_answer(
                "Incident abcdefgh on Camera_01. A worker is visible near a forklift, "
                "but the incident type cannot be inferred from this short clip. "
                "[Markdown Report](https://example.invalid/static/report.md)",
                incident_id="abcdefgh",
                sensor="Camera_01",
                fact="Near Miss",
            )

    def test_missing_report_link_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "artifact link"):
            MODULE.validate_answer(
                "Incident abcdefgh on Camera_01. A worker is visible in a warehouse "
                "aisle, with no further evidence of an incident in this clip.",
                incident_id="abcdefgh",
                sensor="Camera_01",
                fact="worker",
            )

    def test_enriched_label_and_separate_report_are_required_when_expected(self) -> None:
        answer = (
            "Incident d515103ab0428e1db86e1778950b73d0011e8bb6, Camera_01. "
            "The model-generated report sees a worker in an aisle but does not establish an incident. "
            "**Alerts index classification:** Near Miss Violation (verdict: confirmed). "
            "**Model-generated video report:** A worker moves along an aisle. "
            "They are not reconciled here; review the video. "
            "[Markdown Report](https://example.invalid/static/report.md)"
        )
        MODULE.validate_answer(
            answer,
            incident_id="d515103ab0428e1db86e1778950b73d0011e8bb6",
            sensor="Camera_01",
            fact="aisle",
            category="Near Miss Violation",
            verdict="confirmed",
        )
        with self.assertRaisesRegex(ValueError, "exact enriched alert classification"):
            MODULE.validate_answer(
                answer.replace("Near Miss Violation", "Unknown"),
                incident_id="d515103ab0428e1db86e1778950b73d0011e8bb6",
                sensor="Camera_01",
                fact="aisle",
                category="Near Miss Violation",
                verdict="confirmed",
            )
        with self.assertRaisesRegex(ValueError, "separate report assessment"):
            MODULE.validate_answer(
                answer.replace("They are not reconciled here", "These models agree"),
                incident_id="d515103ab0428e1db86e1778950b73d0011e8bb6",
                sensor="Camera_01",
                fact="aisle",
                category="Near Miss Violation",
                verdict="confirmed",
            )

    def test_report_artifact_must_share_scene_fact(self) -> None:
        report = (
            "# Warehouse Incident Report\nSensor ID: Camera_01\n"
            "A pedestrian walks through an aisle near stocked shelves. "
            + "Additional grounded detail. " * 12
        )
        MODULE.validate_report(report, sensor="Camera_01", fact="aisle")
        with self.assertRaisesRegex(ValueError, "scene fact"):
            MODULE.validate_report(report, sensor="Camera_01", fact="Near Miss")

    def test_diagnostic_excerpt_is_bounded_and_masks_credentials(self) -> None:
        text = (
            "Report at https://example.invalid/private?token=sample "
            "Bearer abcdef password:secret-value "
            + "x" * 1000
        )
        excerpt = MODULE.safe_excerpt(text)
        self.assertLessEqual(len(excerpt), 650)
        self.assertNotIn("example.invalid", excerpt)
        self.assertNotIn("abcdef", excerpt)
        self.assertNotIn("secret-value", excerpt)


if __name__ == "__main__":
    unittest.main()
