"""Offline metadata, prompt and local review-package regression checks."""

import copy
from datetime import datetime, timezone
import hashlib
import html
import importlib.util
import json
from pathlib import Path
import re
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch


DIRECTORY = Path(__file__).resolve().parents[1]
PATCH = DIRECTORY / "0004-bind-source-alert-metadata.patch"
SPEC = importlib.util.spec_from_file_location("bound_report_review", DIRECTORY / "prepare-bound-report-review.py")
assert SPEC and SPEC.loader
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)
COLLECTOR_SPEC = importlib.util.spec_from_file_location("bound_report_contract", DIRECTORY / "collect-bound-report-contract.py")
assert COLLECTOR_SPEC and COLLECTOR_SPEC.loader
COLLECTOR = importlib.util.module_from_spec(COLLECTOR_SPEC)
COLLECTOR_SPEC.loader.exec_module(COLLECTOR)
INCIDENT = "0fe88cf6816b424dd09fc76eeea63fe6919c54e6"
CATEGORY = "Person walking — restart test"
NARRATIVE = "## Incident Details\n\nA person walks along the aisle. Vehicle motion is not established.\n\n##Resources\n\nhttps://warehouse.example/clip.mp4\n"
GENERATED = "# Warehouse Incident Report\n\n## Basic Information\n\n| Report Identifier | Unknown |\n| Date of Incident | Unknown |\n\n" + NARRATIVE


def load_binder():
    lines = []
    collecting = False
    for line in PATCH.read_text().splitlines():
        if line.startswith("+def _bind_alert_metadata("):
            collecting = True
        if collecting:
            if not line.startswith("+"):
                break
            lines.append(line[1:])
    if not lines:
        raise ValueError("Metadata helper is missing from the patch")
    scope = {"TemplateReportGenInput": SimpleNamespace, "Any": object,
             "datetime": datetime, "timezone": timezone, "html": html, "re": re}
    exec(compile("\n".join(lines), str(PATCH), "exec"), scope)
    return scope["_bind_alert_metadata"]


def report_input():
    return SimpleNamespace(
        alert_sensor_id="Camera",
        alert_from_timestamp="2026-11-04T22:59:54.920Z",
        alert_to_timestamp="2026-11-04T23:00:24.854Z",
        alert_metadata={"Id": INCIDENT, "sensorId": "Camera", "timestamp": "2026-11-04T22:59:54.920Z",
                        "end": "2026-11-04T23:00:24.854Z", "category": CATEGORY,
                        "info": {"verdict": "confirmed"}},
    )


class BoundMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bind = staticmethod(load_binder())

    def test_known_identity_time_category_and_verdict_replace_unknown_fields(self):
        result = self.bind(GENERATED, report_input())
        for value in (INCIDENT, "2026-11-04", "22:59:54.92 UTC", "2026-11-04T22:59:54.920000Z",
                      "2026-11-04T23:00:24.854000Z", "Camera", CATEGORY, "confirmed"):
            self.assertIn(value, result)
        self.assertNotIn("| Unknown |", result)
        self.assertIn("Source: stored alert metadata.", result)

    def test_narrative_and_resource_links_are_preserved_byte_for_byte(self):
        result = self.bind(GENERATED, report_input())
        self.assertTrue(result.endswith(NARRATIVE))
        self.assertIn("Model-generated visual assessment follows", result)
        self.assertIn("separate from the stored alert classification", result)

    def test_binding_is_idempotent_and_does_not_mutate_input(self):
        value = report_input()
        before = copy.deepcopy(value)
        first = self.bind(GENERATED, value)
        self.assertEqual(self.bind(first, value), first)
        self.assertEqual(value.__dict__, before.__dict__)

    def test_missing_wrong_or_conflicting_incident_ids_fail(self):
        for metadata in ({}, {"Id": "unknown"}, {"Id": INCIDENT, "id": "another-valid-id"}):
            value = report_input()
            value.alert_metadata.update(metadata)
            if not metadata:
                value.alert_metadata.pop("Id")
            with self.subTest(metadata=metadata), self.assertRaisesRegex(ValueError, "exact source incident"):
                self.bind(GENERATED, value)

    def test_lowercase_id_field_is_supported_when_unambiguous(self):
        value = report_input()
        value.alert_metadata["id"] = value.alert_metadata.pop("Id")
        self.assertIn(INCIDENT, self.bind(GENERATED, value))

    def test_sensor_mismatch_or_unsafe_sensor_fails(self):
        for sensor in ("Camera_01", "Camera\n## injected"):
            value = report_input()
            value.alert_sensor_id = sensor
            with self.subTest(sensor=sensor), self.assertRaisesRegex(ValueError, "sensor"):
                self.bind(GENERATED, value)

    def test_bad_missing_timezone_mismatched_and_reversed_intervals_fail(self):
        changes = (
            ("alert_from_timestamp", "not-a-date"),
            ("alert_from_timestamp", "2026-11-04T22:59:54.920"),
            ("alert_from_timestamp", "2026-11-04T22:59:55.920Z"),
            ("alert_to_timestamp", "2026-11-04T22:00:00Z"),
        )
        for field, text in changes:
            value = report_input()
            setattr(value, field, text)
            with self.subTest(field=field, text=text), self.assertRaises(ValueError):
                self.bind(GENERATED, value)
        value = report_input()
        value.alert_metadata.pop("timestamp")
        with self.assertRaisesRegex(ValueError, "timestamps"):
            self.bind(GENERATED, value)

    def test_equivalent_timezone_values_are_rendered_in_utc(self):
        value = report_input()
        value.alert_from_timestamp = "2026-11-04T14:59:54.920-08:00"
        self.assertIn("22:59:54.92 UTC", self.bind(GENERATED, value))

    def test_category_is_required_and_verdict_absence_is_not_invented(self):
        value = report_input()
        value.alert_metadata.pop("category")
        with self.assertRaisesRegex(ValueError, "category"):
            self.bind(GENERATED, value)
        value = report_input()
        value.alert_metadata["info"] = {}
        result = self.bind(GENERATED, value)
        self.assertIn("Not provided in alert metadata", result)
        self.assertNotIn("confirmed", result)

    def test_table_markup_is_escaped_and_control_characters_fail(self):
        value = report_input()
        value.alert_metadata["category"] = "People | <script>"
        result = self.bind(GENERATED, value)
        self.assertIn("People &#124; &lt;script&gt;", result)
        value.alert_metadata["category"] = "People\n## Other"
        with self.assertRaisesRegex(ValueError, "control"):
            self.bind(GENERATED, value)

    def test_missing_or_duplicate_basic_information_section_fails(self):
        for generated in (NARRATIVE, GENERATED + "\n## Basic Information\n\nUnknown\n"):
            with self.subTest(generated_length=len(generated)), self.assertRaisesRegex(ValueError, "exactly one"):
                self.bind(generated, report_input())

    def test_binding_precedes_both_markdown_and_pdf_saves(self):
        text = PATCH.read_text()
        self.assertIn("+        markdown_content = _bind_alert_metadata(markdown_content, report_input)", text)
        self.assertIn(" # Generate filenames", text)
        self.assertNotIn("_save_markdown_to_object_store(", text)
        self.assertNotIn("_save_pdf_to_object_store(", text)
        self.assertEqual(text.count('+            tool_call_args = {"id": report_input.incident_id, "includes": ["objectIds", "info", "category"]}'), 1)
        self.assertIn('raise ValueError("Incident lookup returned a different incident ID")', text)

    def test_prompt_change_is_observation_only_and_preserves_model_uncertainty(self):
        prompt = (DIRECTORY / "0005-observation-only-report-prompts.patch").read_text()
        added = "\n".join(line[1:] for line in prompt.splitlines() if line.startswith("+") and not line.startswith("+++"))
        self.assertIn("position visibly changes across frames", added)
        self.assertIn("Do not infer", added)
        self.assertIn("State uncertainty", added)
        self.assertIn("Unknown", added)
        self.assertNotIn("llms:", added)
        self.assertNotIn("base_url:", added)
        self.assertNotIn("reasoning:", added)

    def test_optional_vehicle_template_patch_is_one_placeholder_only(self):
        text = (DIRECTORY / "0006-optional-vehicle-count-template.patch").read_text()
        changes = [line for line in text.splitlines() if line[:1] in ("+", "-") and not line.startswith(("+++", "---"))]
        self.assertEqual(changes, ["-| **Number of Vehicles involved** | {persons_involved} |", "+| **Number of Vehicles involved** | {vehicles_involved} |"])


class ReviewPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.files = {}
        self.hashes = {}
        for name, (relative, _digest) in BUILDER.SOURCE_FILES.items():
            content = b"pass\n" if name.endswith(".py") else b"example content\n"
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            self.files[name] = (relative, digest)
            self.hashes[name] = digest
        self.hashes["va_mcp_server_config.yml"] = BUILDER.RUNTIME_HASHES["va_mcp_server_config.yml"]
        self.contract = {
            "namespace": "nvidia-vss-321-warehouse", "deployment": "warehouse-demo-agent",
            "uid": "example-deployment-uid", "resourceVersion": "1234", "image": BUILDER.IMAGE,
            "resources": {"requests": {"cpu": "500m", "memory": "1Gi"}, "limits": {"cpu": "2", "memory": "4Gi"}},
            "nodeName": None, "runtime_sha256": self.hashes,
            "volumeMounts": [{"name": "agent-report-content", "mountPath": BUILDER.MOUNT_PATHS["report_agent.py"], "subPath": "report_agent.py", "readOnly": True},
                             {"name": "agent-config", "mountPath": "/vss-agent/config", "readOnly": True}],
            "configMapVolumes": [{"name": "agent-report-content", "configMap": {"name": "warehouse-agent-report-content-v2"}},
                                 {"name": "agent-config", "configMap": {"name": "warehouse-agent"}}],
        }
        self.output = self.root / "review"

    def build(self, **kwargs):
        self.calls = []

        def fake_run(command, **_kwargs):
            self.calls.append(command)
            return SimpleNamespace(returncode=0)

        with patch.object(BUILDER, "SOURCE_FILES", self.files), patch.object(BUILDER, "RUNTIME_HASHES", self.hashes), patch.object(BUILDER.subprocess, "run", fake_run):
            return BUILDER.prepare(source=self.source, output=self.output, live_contract=self.contract, **kwargs)

    def test_metadata_only_defaults_and_preserved_image_resources(self):
        review = self.build()
        self.assertEqual(review["image_before"], review["image_after"])
        self.assertEqual(review["resources_before"], review["resources_after"])
        self.assertEqual(review["optional_changes"], {"observation_only_report_prompts": False, "vehicle_count_template_placeholder": False})
        cm = json.loads((self.output / "configmap.json").read_text())
        self.assertTrue(cm["immutable"])
        self.assertEqual(set(cm["data"]), {"report_agent.py", "template_report_gen.py"})
        self.assertEqual(len(self.calls), 6)
        self.assertTrue(all(call[:2] == ["git", "apply"] for call in self.calls))

    def test_opt_ins_are_separate_and_overlay_changes_only_mounts(self):
        self.build(observation_prompts=True, template_count_fix=True)
        overlay = json.loads((self.output / "agent-overlay.json").read_text())
        spec = overlay["spec"]["template"]["spec"]
        self.assertEqual(set(spec), {"containers", "volumes"})
        self.assertEqual(set(spec["containers"][0]), {"name", "volumeMounts"})
        self.assertEqual(len(spec["containers"][0]["volumeMounts"]), 4)
        self.assertNotIn("resources", spec["containers"][0])
        self.assertNotIn("image", spec["containers"][0])
        self.assertNotIn("nodeName", spec)

    def test_prompt_config_uses_one_projected_directory_not_nested_subpath(self):
        self.build(observation_prompts=True)
        overlay = json.loads((self.output / "agent-overlay.json").read_text())
        self.assertEqual((self.output / "agent-overlay.json").read_bytes(), (self.output / "corrective-overlay.json").read_bytes())
        spec = overlay["spec"]["template"]["spec"]
        mounts = spec["containers"][0]["volumeMounts"]
        actual = [value for value in mounts if value.get("$patch") != "delete"]
        self.assertEqual(len(actual), 2)
        self.assertTrue(all(value["subPath"].endswith(".py") for value in actual))
        self.assertIn({"mountPath": "/vss-agent/config/config.yml", "$patch": "delete"}, mounts)
        config = next(value for value in spec["volumes"] if value["name"] == "agent-config")
        self.assertIsNone(config["configMap"])
        sources = config["projected"]["sources"]
        original = sources[0]["configMap"]
        replacement = sources[1]["configMap"]
        self.assertEqual(original["name"], "warehouse-agent")
        self.assertEqual({value["key"] for value in original["items"]}, {"incident_report_template.md", "va_mcp_server_config.yml"})
        self.assertEqual(replacement["name"], "warehouse-agent-bound-report-v1")
        self.assertEqual(replacement["items"], [{"key": "config.yml", "path": "config.yml"}])
        paths = [item["path"] for source in sources for item in source["configMap"]["items"]]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(set(paths), set(BUILDER.BASE_CONFIG_FILES))

    def test_projected_config_requires_original_preserved_file_hashes(self):
        self.contract["runtime_sha256"] = {**self.hashes, "va_mcp_server_config.yml": "wrong"}
        with self.assertRaisesRegex(ValueError, "Preserved Agent configuration hash"):
            self.build(observation_prompts=True)
        self.assertFalse(self.output.exists())

    def test_parent_config_mount_and_volume_are_required(self):
        self.contract["volumeMounts"] = self.contract["volumeMounts"][:1]
        with self.assertRaisesRegex(ValueError, "read-only Agent configuration directory"):
            self.build(observation_prompts=True)
        self.assertFalse(self.output.exists())

    def test_wrong_live_hash_or_image_is_rejected_before_output(self):
        self.contract["image"] = "unapproved-image"
        with self.assertRaisesRegex(ValueError, "image"):
            self.build()
        self.assertFalse(self.output.exists())
        self.contract["image"] = BUILDER.IMAGE
        self.contract["runtime_sha256"] = {"report_agent.py": "wrong"}
        with self.assertRaisesRegex(ValueError, "hash"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_modified_public_source_is_rejected_before_output(self):
        path = self.source / self.files["report_agent.py"][0]
        path.write_text("modified source\n")
        with self.assertRaisesRegex(ValueError, "Public pinned source hash"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_existing_output_is_never_overwritten(self):
        self.output.mkdir()
        sentinel = self.output / "keep.txt"
        sentinel.write_text("retained")
        with self.assertRaisesRegex(ValueError, "never overwritten"):
            self.build()
        self.assertEqual(sentinel.read_text(), "retained")

    def test_rollback_restores_v2_mount_and_removes_only_added_mounts(self):
        self.build(observation_prompts=True, template_count_fix=True)
        rollback = json.loads((self.output / "agent-rollback.json").read_text())
        spec = rollback["spec"]["template"]["spec"]
        mounts = spec["containers"][0]["volumeMounts"]
        self.assertEqual(mounts[0]["name"], "agent-report-content")
        self.assertEqual({x["mountPath"] for x in mounts[1:]}, {BUILDER.MOUNT_PATHS[name] for name in ("template_report_gen.py", "config.yml", "incident_report_template.md")})
        self.assertTrue(all(x["$patch"] == "delete" for x in mounts[1:]))
        self.assertEqual(spec["volumes"][0], {"name": "agent-bound-report", "$patch": "delete"})
        self.assertEqual(spec["volumes"][1], {"name": "agent-config", "projected": None, "configMap": {"name": "warehouse-agent", "defaultMode": 420}})


class ReadOnlyContractTests(unittest.TestCase):
    def test_collector_returns_only_whitelisted_metadata_and_hashes(self):
        deployment = {
            "metadata": {"uid": "retained-deployment-uid", "resourceVersion": "12", "annotations": {"unrelated": "not-returned"}},
            "spec": {"template": {"spec": {
                "containers": [{"name": "agent", "image": BUILDER.IMAGE, "resources": {"limits": {"cpu": "2"}},
                                "env": [{"name": "UNRELATED_VALUE", "value": "not-returned"}],
                                "volumeMounts": [{"name": "agent-config", "mountPath": "/vss-agent/config", "readOnly": True},
                                                 {"name": "unrelated-volume", "mountPath": "/other"}]}],
                "volumes": [{"name": "agent-config", "configMap": {"name": "warehouse-agent"}},
                            {"name": "unrelated-volume", "secret": {"secretName": "not-returned"}}],
            }}},
        }
        commands = []

        def fake_run(*args):
            commands.append(args)
            return json.dumps(deployment if "get" in args else {name: "0" * 64 for name in COLLECTOR.PATHS})

        with patch.object(COLLECTOR, "run", fake_run):
            contract = COLLECTOR.collect("nvidia-vss-321-warehouse", "warehouse-demo-agent")
        self.assertEqual(len(commands), 2)
        self.assertIn("get", commands[0])
        self.assertIn("exec", commands[1])
        self.assertNotIn("not-returned", json.dumps(contract))
        self.assertNotIn("secret", json.dumps(contract))
        self.assertNotIn("env", contract)
        self.assertEqual(set(contract["runtime_sha256"]), set(COLLECTOR.PATHS))

    def test_collector_remote_program_outputs_hashes_not_source(self):
        text = (DIRECTORY / "collect-bound-report-contract.py").read_text()
        self.assertIn("hashlib.sha256(open(v,'rb').read()).hexdigest()", text)
        self.assertNotIn("check_output", text)
        self.assertNotIn("shell=True", text)
        self.assertNotIn('"apply"', text)
        self.assertNotIn('"patch"', text)


if __name__ == "__main__":
    unittest.main()
