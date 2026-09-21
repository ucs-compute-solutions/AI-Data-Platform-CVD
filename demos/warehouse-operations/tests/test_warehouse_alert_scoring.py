from __future__ import annotations

import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


WAREHOUSE_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = WAREHOUSE_ROOT / "scripts" / "warehouse_alert_scoring.py"
MANIFEST_PATH = WAREHOUSE_ROOT / "data" / "warehouse-data-manifest.yaml"
SPEC = importlib.util.spec_from_file_location("warehouse_alert_scoring", SCRIPT_PATH)
assert SPEC and SPEC.loader
SCORING = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCORING)


class WarehouseAlertScoringTests(unittest.TestCase):
    def test_repository_manifest_validates_without_pyyaml(self) -> None:
        manifest = SCORING.load_manifest(MANIFEST_PATH)
        result = SCORING.validate_manifest_document(manifest)
        self.assertTrue(result["valid"])
        self.assertEqual(result["camera_count"], 4)
        self.assertEqual(result["positive_alert_count"], 0)
        self.assertEqual(result["scenario_status"], "pending")

    def test_init_workspace_is_non_overwriting_and_has_unscored_timelines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "score"
            SCORING.init_workspace(MANIFEST_PATH, output, ["reviewer-a", "reviewer-b"])
            reviewer_rows = SCORING.read_csv_rows(
                output / "reviewer-a-labels.csv", SCORING.HUMAN_LABEL_COLUMNS
            )
            self.assertEqual(len(reviewer_rows), 4)
            self.assertTrue(all(row["record_type"] == "timeline_review" for row in reviewer_rows))
            self.assertTrue(all(row["timeline_reviewed"] == "no" for row in reviewer_rows))
            self.assertTrue(all(not row["label"] for row in reviewer_rows))
            with self.assertRaises(SCORING.ScoringError):
                SCORING.init_workspace(MANIFEST_PATH, output, ["reviewer-a", "reviewer-b"])

    def test_ingest_whitelists_fields_and_does_not_copy_raw_secret_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "alerts.jsonl"
            source.write_text(
                json.dumps(
                    {
                        "sensorId": "Camera",
                        "alertType": "Near Miss Violation",
                        "start_seconds": 10,
                        "end_seconds": 12,
                        "verdict": "confirmed",
                        "alertId": "alert-1",
                        "token": "must-not-be-copied",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            output = root / "evidence.csv"
            imported = SCORING.ingest_evidence(
                source,
                output,
                input_format="jsonl",
                mappings={},
                run_id="run-1",
                iteration=1,
                evidence_type="alerts_ui",
                default_sensor="",
                default_rule="",
            )
            self.assertEqual(imported, 1)
            rows = SCORING.read_csv_rows(output, SCORING.EVIDENCE_COLUMNS)
            self.assertEqual(rows[0]["rule"], "near_miss")
            self.assertEqual(rows[0]["alert_id"], "alert-1")
            self.assertNotIn("must-not-be-copied", output.read_text(encoding="utf-8"))

    def test_near_miss_requires_and_passes_complete_three_run_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reviewer_paths, metadata_path, ground_truth_path, evidence_path, scenario_path = self._passing_fixture(root)
            result = SCORING.score(
                MANIFEST_PATH,
                metadata_path,
                reviewer_paths,
                ground_truth_path,
                evidence_path,
                scenario_path,
            )
            self.assertTrue(result["reproducibility_gate"]["passed"])
            self.assertEqual(result["reproducibility_gate"]["qualified_scenarios"], ["near-miss-1"])
            self.assertEqual(result["metrics"]["aggregate"]["tp"], 3)
            self.assertEqual(result["metrics"]["aggregate"]["fp"], 0)
            self.assertEqual(result["metrics"]["aggregate"]["fn"], 0)

    def test_two_runs_do_not_pass_reproducibility_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reviewer_paths, metadata_path, ground_truth_path, evidence_path, scenario_path = self._passing_fixture(root)
            rows = SCORING.read_csv_rows(scenario_path, SCORING.SCENARIO_RUN_COLUMNS)
            SCORING.write_csv(scenario_path, SCORING.SCENARIO_RUN_COLUMNS, rows[:2])
            result = SCORING.score(
                MANIFEST_PATH,
                metadata_path,
                reviewer_paths,
                ground_truth_path,
                evidence_path,
                scenario_path,
            )
            gate = result["reproducibility_gate"]
            self.assertFalse(gate["passed"])
            self.assertIn("exactly one row", " ".join(gate["scenario_results"][0]["errors"]))

    def test_unverified_input_hashes_block_an_otherwise_complete_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reviewer_paths, metadata_path, ground_truth_path, evidence_path, scenario_path = self._passing_fixture(root)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["source_hashes_verified"] = False
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            result = SCORING.score(
                MANIFEST_PATH,
                metadata_path,
                reviewer_paths,
                ground_truth_path,
                evidence_path,
                scenario_path,
            )
            gate = result["reproducibility_gate"]
            self.assertFalse(gate["passed"])
            self.assertEqual(gate["chain_qualified_scenarios"], ["near-miss-1"])
            self.assertEqual(gate["qualified_scenarios"], [])
            self.assertIn("source_hashes_verified is not true", gate["input_lock"]["errors"])

    def _passing_fixture(
        self, root: Path
    ) -> tuple[list[Path], Path, Path, Path, Path]:
        SCORING.init_workspace(MANIFEST_PATH, root, ["reviewer-a", "reviewer-b"])
        reviewer_paths = [root / "reviewer-a-labels.csv", root / "reviewer-b-labels.csv"]
        for index, reviewer_path in enumerate(reviewer_paths, start=1):
            reviewer_rows = SCORING.read_csv_rows(
                reviewer_path, SCORING.HUMAN_LABEL_COLUMNS
            )
            for row in reviewer_rows:
                row["timeline_reviewed"] = "yes"
                row["review_completed_utc"] = f"2026-09-21T1{index}:00:00Z"
            reviewer_rows.append(
                {
                    "record_type": "candidate",
                    "label_id": f"R{index}-1",
                    "reviewer_id": f"reviewer-{'a' if index == 1 else 'b'}",
                    "sensor_id": "Camera",
                    "source_file": "videos/nv-warehouse-4cams/Camera.mp4",
                    "rule": "near_miss",
                    "label": "positive",
                    "start_seconds": "10",
                    "end_seconds": "12",
                    "visible_facts": "A worker and moving forklift pass within the labeled interval.",
                }
            )
            SCORING.write_csv(reviewer_path, SCORING.HUMAN_LABEL_COLUMNS, reviewer_rows)
        metadata_path = root / "run-metadata.json"
        ground_truth_path = root / "adjudicated-labels.csv"
        evidence_path = root / "evidence.csv"
        scenario_path = root / "scenario-runs.csv"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata.update(
            {
                "status": "ready_for_scoring",
                "source_hashes_verified": True,
                "configuration_hashes_verified": True,
                "archive_hash_verified": True,
                "image_digests": [
                    {
                        "component": "warehouse-test-fixture",
                        "image": "registry.example/warehouse@test-fixture@sha256:"
                        + "b" * 64,
                    }
                ],
                "model_ids": [
                    {"role": "perception", "model_id": "fixture/perception"},
                    {"role": "vlm", "model_id": "fixture/vlm"},
                    {"role": "agent_llm", "model_id": "fixture/agent"},
                ],
                "run_ids": ["run-1", "run-2", "run-3"],
            }
        )
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        SCORING.write_csv(
            ground_truth_path,
            SCORING.ADJUDICATED_COLUMNS,
            [
                {
                    "ground_truth_id": "GT-near-miss-1",
                    "sensor_id": "Camera",
                    "source_file": "videos/nv-warehouse-4cams/Camera.mp4",
                    "rule": "near_miss",
                    "label": "positive",
                    "start_seconds": "10",
                    "end_seconds": "12",
                    "visible_facts": "A worker and moving forklift pass within the labeled interval.",
                    "reviewer_label_ids": "R1-1;R2-1",
                    "adjudication_status": "accepted",
                    "adjudicator_ids": "reviewer-a;reviewer-b",
                    "adjudicated_utc": "2026-09-21T12:00:00Z",
                }
            ],
        )
        evidence_rows: list[dict[str, str]] = []
        scenario_rows: list[dict[str, str]] = []
        for iteration in (1, 2, 3):
            run_id = f"run-{iteration}"
            alert_id = f"alert-{iteration}"
            incident_id = f"incident-{iteration}"

            def evidence(identifier: str, evidence_type: str, **values: str) -> dict[str, str]:
                row = {column: "" for column in SCORING.EVIDENCE_COLUMNS}
                row.update(
                    {
                        "evidence_id": identifier,
                        "run_id": run_id,
                        "replay_iteration": str(iteration),
                        "evidence_type": evidence_type,
                        "sensor_id": "Camera",
                        "rule": "near_miss",
                        "source_export": f"fixture-{iteration}.json",
                        "source_sha256": "a" * 64,
                        "source_record": "1",
                    }
                )
                row.update(values)
                return row

            perception_id = f"EV-perception-{iteration}"
            behavior_id = f"EV-behavior-{iteration}"
            vlm_id = f"EV-vlm-{iteration}"
            delivery_id = f"EV-delivery-{iteration}"
            ui_id = f"EV-ui-{iteration}"
            clip_id = f"EV-clip-{iteration}"
            report_id = f"EV-report-{iteration}"
            health_id = f"EV-health-{iteration}"
            evidence_rows.extend(
                [
                    evidence(perception_id, "perception", track_id=f"track-{iteration}"),
                    evidence(behavior_id, "behavior", incident_id=incident_id),
                    evidence(vlm_id, "vlm", incident_id=incident_id, verdict="confirmed"),
                    evidence(
                        delivery_id,
                        "delivery",
                        incident_id=incident_id,
                        alert_id=alert_id,
                        status="delivered",
                    ),
                    evidence(
                        ui_id,
                        "alerts_ui",
                        incident_id=incident_id,
                        alert_id=alert_id,
                        verdict="confirmed",
                        source_start_seconds="10",
                        source_end_seconds="12",
                    ),
                    evidence(clip_id, "clip", alert_id=alert_id, clip_playable="true"),
                    evidence(
                        report_id,
                        "agent_report",
                        alert_id=alert_id,
                        report_grounded="true",
                    ),
                    evidence(health_id, "health", health_ok="true"),
                ]
            )
            scenario_rows.append(
                {
                    "scenario_id": "near-miss-1",
                    "ground_truth_id": "GT-near-miss-1",
                    "run_id": run_id,
                    "replay_iteration": str(iteration),
                    "prediction_evidence_id": ui_id,
                    "perception_evidence_ids": perception_id,
                    "behavior_evidence_ids": behavior_id,
                    "vlm_evidence_ids": vlm_id,
                    "delivery_evidence_id": delivery_id,
                    "alerts_ui_evidence_id": ui_id,
                    "clip_evidence_id": clip_id,
                    "agent_report_evidence_id": report_id,
                    "health_evidence_id": health_id,
                }
            )
        SCORING.write_csv(evidence_path, SCORING.EVIDENCE_COLUMNS, evidence_rows)
        SCORING.write_csv(scenario_path, SCORING.SCENARIO_RUN_COLUMNS, scenario_rows)
        return reviewer_paths, metadata_path, ground_truth_path, evidence_path, scenario_path


if __name__ == "__main__":
    unittest.main()
