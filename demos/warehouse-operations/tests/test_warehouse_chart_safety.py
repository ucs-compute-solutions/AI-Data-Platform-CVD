"""Offline contract tests for the NVIDIA Warehouse OpenShift chart skeleton.

These tests inspect only checked-in files.  They do not run ``oc``, contact a
registry, render against a cluster, or mutate any Kubernetes resource.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
WAREHOUSE_ROOT = REPO_ROOT / "demos" / "warehouse-operations"
CHART_ROOT = WAREHOUSE_ROOT / "openshift" / "nvidia-warehouse" / "chart"
VALUES = CHART_ROOT / "values.yaml"
SCHEMA = CHART_ROOT / "values.schema.json"
CHART = CHART_ROOT / "Chart.yaml"
TEMPLATES = CHART_ROOT / "templates"
SOURCE_LOCK = WAREHOUSE_ROOT / "openshift" / "nvidia-warehouse" / "source-lock.yaml"


def load_yaml(path: Path):
    """Load YAML through the system Ruby parser without a Python dependency."""

    ruby = shutil.which("ruby")
    if not ruby:
        raise RuntimeError("ruby is required for the offline YAML contract tests")
    program = (
        "require 'yaml'; require 'json'; "
        "value = YAML.safe_load(File.read(ARGV.fetch(0)), aliases: false); "
        "STDOUT.write(JSON.generate(value))"
    )
    completed = subprocess.run(
        [ruby, "-e", program, str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def image_reference(image: dict) -> str:
    return f"{image['repository']}:{image['tag']}"


class WarehouseChartSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.values = load_yaml(VALUES)
        cls.source_lock = load_yaml(SOURCE_LOCK)
        cls.chart = load_yaml(CHART)
        cls.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        cls.template_text = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(TEMPLATES.glob("*.yaml"))
        }
        cls.helpers = (TEMPLATES / "_helpers.tpl").read_text(encoding="utf-8")
        cls.readme = (CHART_ROOT / "README.md").read_text(encoding="utf-8")

    def test_defaults_are_inert_and_require_explicit_acknowledgements(self):
        global_values = self.values["global"]
        self.assertIs(global_values["enabled"], False)
        self.assertIs(global_values["acknowledgeIncomplete"], False)
        self.assertIs(global_values["allowGpuPlaceholders"], False)
        self.assertIs(global_values["allowTagOnlyRender"], False)

        for section in ("statefulServices", "cpuServices", "gpuWorkloads"):
            with self.subTest(section=section):
                self.assertTrue(self.values[section], f"{section} must not be empty")
                self.assertTrue(
                    all(component["enabled"] is False for component in self.values[section].values()),
                    f"every checked-in {section} component must be disabled",
                )

        for section in ("configMaps", "persistentVolumeClaims", "initializationJobs", "routes"):
            with self.subTest(section=section):
                self.assertTrue(self.values[section], f"{section} must not be empty")
                self.assertTrue(
                    all(component["enabled"] is False for component in self.values[section].values()),
                    f"every checked-in {section} entry must be disabled",
                )

        self.assertTrue(
            all(config["data"] == {} for config in self.values["configMaps"].values()),
            "configuration payloads must stay empty until copied from the locked source",
        )
        self.assertTrue(
            all(
                job["implementationStatus"] != "render-ready" and job["command"] == []
                for job in self.values["initializationJobs"].values()
            ),
            "initialization Jobs must stay blocked until their exact implementation is supplied",
        )
        self.assertTrue(
            all(reference["name"] == "" for reference in self.values["secretReferences"].values()),
            "checked-in values must not name site Secret objects",
        )
        self.assertIs(self.values["networkPolicies"]["enabled"], False)
        self.assertIs(self.values["networkPolicies"]["acknowledgeIncomplete"], False)

        global_schema = self.schema["properties"]["global"]
        for key in (
            "enabled",
            "acknowledgeIncomplete",
            "allowGpuPlaceholders",
            "allowTagOnlyRender",
        ):
            self.assertIn(key, global_schema["required"])
            self.assertEqual(global_schema["properties"][key]["type"], "boolean")

        self.assertIn("global.enabled=true requires global.acknowledgeIncomplete=true", self.helpers)

    def test_schema_covers_every_checked_in_top_level_value(self):
        value_keys = set(self.values)
        property_keys = set(self.schema["properties"])
        required_keys = set(self.schema["required"])
        self.assertEqual(property_keys, value_keys)
        self.assertEqual(required_keys, value_keys)
        self.assertIs(self.schema["additionalProperties"], False)

    def test_every_resource_template_is_behind_the_global_gate(self):
        resource_templates = {
            name: text for name, text in self.template_text.items() if "apiVersion:" in text
        }
        self.assertTrue(resource_templates)
        for name, text in resource_templates.items():
            with self.subTest(template=name):
                api_position = text.index("apiVersion:")
                gate_positions = [
                    position
                    for expression in (
                        "{{- if .Values.global.enabled }}",
                        "{{- if and .Values.global.enabled",
                        "{{- if $.Values.global.enabled }}",
                    )
                    if (position := text.find(expression)) >= 0
                ]
                self.assertTrue(gate_positions, f"{name} has no global.enabled gate")
                self.assertLess(min(gate_positions), api_position)

    def test_component_services_are_independently_gated(self):
        cpu = self.template_text["cpu-services.yaml"]
        stateful = self.template_text["stateful-services.yaml"]
        gpu = self.template_text["gpu-workloads.yaml"]
        routes = self.template_text["routes.yaml"]
        network_policies = self.template_text["networkpolicies.yaml"]

        self.assertIn("{{- if $component.enabled }}", cpu)
        self.assertIn("{{- if $component.service.enabled }}", cpu)
        self.assertIn("{{- if $component.enabled }}", stateful)
        self.assertIn("{{- if $component.enabled }}", gpu)
        self.assertIn("global.allowGpuPlaceholders=true", gpu)

        for key, route in self.values["routes"].items():
            with self.subTest(route=key):
                self.assertIs(route["enabled"], False)
                self.assertEqual(route["host"], "")
        self.assertIn("{{- if .Values.global.enabled }}", routes)
        self.assertIn("{{- if $route.enabled }}", routes)
        self.assertIn('routes.%s.host is required', routes)

        policy_values = self.values["networkPolicies"]
        self.assertIs(policy_values["enabled"], False)
        self.assertIs(policy_values["acknowledgeIncomplete"], False)
        self.assertIs(policy_values["allowSameRelease"], False)
        self.assertIs(policy_values["dns"]["enabled"], False)
        self.assertIs(policy_values["routerIngress"]["enabled"], False)
        self.assertIn(
            "if and .Values.global.enabled .Values.networkPolicies.enabled",
            network_policies,
        )
        self.assertIn(
            "networkPolicies.enabled=true requires networkPolicies.acknowledgeIncomplete=true",
            network_policies,
        )
        self.assertIn("networkPolicies.dns.namespaceSelector is required", network_policies)
        self.assertIn("networkPolicies.dns.podSelector is required", network_policies)
        self.assertIn(
            "networkPolicies.routerIngress.namespaceSelector is required",
            network_policies,
        )
        self.assertIn(
            "networkPolicies.routerIngress.podSelector is required",
            network_policies,
        )

    def test_immutable_image_guard_is_fail_closed(self):
        self.assertIn("{{- if .image.digest -}}", self.helpers)
        self.assertIn('printf "%s@%s" $repository .image.digest', self.helpers)
        self.assertIn("if not .root.Values.global.allowTagOnlyRender", self.helpers)
        self.assertIn("enabled components require immutable image digests", self.helpers)
        self.assertIn('if eq $tag "latest"', self.helpers)
        self.assertIn("the image tag latest is prohibited", self.helpers)

        image_schema = self.schema["definitions"]["image"]
        self.assertEqual(
            image_schema["properties"]["digest"]["pattern"],
            "^$|^sha256:[a-f0-9]{64}$",
        )
        for key, image in self.values["images"].items():
            with self.subTest(image=key):
                self.assertNotEqual(str(image["tag"]).lower(), "latest")
                digest = image["digest"]
                self.assertTrue(
                    digest == "" or re.fullmatch(r"sha256:[a-f0-9]{64}", digest),
                    f"invalid digest for images.{key}",
                )

    def test_gpu_controllers_render_only_in_zero_replica_standby(self):
        gpu_schema = self.schema["definitions"]["gpuWorkload"]
        self.assertEqual(gpu_schema["properties"]["standbyReplicas"], {"enum": [0]})
        self.assertEqual(gpu_schema["properties"]["activeReplicas"]["minimum"], 1)

        for key, workload in self.values["gpuWorkloads"].items():
            with self.subTest(workload=key):
                self.assertEqual(workload["standbyReplicas"], 0)
                self.assertGreaterEqual(workload["activeReplicas"], 1)
                self.assertLessEqual(workload["gpuCount"], 8)

        gpu_template = self.template_text["gpu-workloads.yaml"]
        self.assertIn("replicas: {{ $component.standbyReplicas }}", gpu_template)
        self.assertNotIn("replicas: {{ $component.activeReplicas }}", gpu_template)
        self.assertIn("warehouse.nvidia.com/active-replicas", gpu_template)

    def test_chart_contains_no_secret_payload_or_secret_resource(self):
        inspected_files = [VALUES, SCHEMA, CHART, *sorted(TEMPLATES.iterdir())]
        inspected_text = "\n".join(
            path.read_text(encoding="utf-8") for path in inspected_files if path.is_file()
        )

        self.assertNotRegex(inspected_text, r"(?m)^kind:\s*Secret\s*$")
        self.assertNotRegex(inspected_text, r"(?m)^\s*stringData:\s*$")
        self.assertNotRegex(inspected_text, r"(?m)^\s*\.dockerconfigjson:\s*")
        self.assertNotRegex(
            inspected_text,
            r"(?im)^\s*(password|passwd|api[_-]?key|token|private[_-]?key)\s*:\s*\S+",
        )

        # Pull-secret entries are references by name only; their data is not
        # accepted by this values schema.
        pull_secret_schema = self.schema["properties"]["global"]["properties"][
            "imagePullSecrets"
        ]["items"]
        self.assertEqual(set(pull_secret_schema["properties"]), {"name"})
        self.assertIs(pull_secret_schema["additionalProperties"], False)

    def test_chart_metadata_and_images_align_with_exact_source_lock(self):
        source = self.source_lock["source"]
        values_source = self.values["global"]["source"]
        annotations = self.chart["annotations"]

        self.assertEqual(values_source["tag"], source["tag"])
        self.assertEqual(values_source["commit"], source["commit"])
        self.assertEqual(values_source["releaseMetadata"], source["release_metadata"])
        self.assertEqual(annotations["warehouse.nvidia.com/source-tag"], source["tag"])
        self.assertEqual(annotations["warehouse.nvidia.com/source-commit"], source["commit"])
        self.assertEqual(self.chart["appVersion"], source["tag"].removeprefix("v"))

        chart_images = {image_reference(image) for image in self.values["images"].values()}
        locked_images = set(self.source_lock["images"])
        self.assertFalse(chart_images - locked_images, "chart contains an image outside source lock")

        resolution = self.source_lock["image_digest_resolution"]
        for image in self.values["images"].values():
            reference = image_reference(image)
            with self.subTest(reference=reference):
                if image["digest"]:
                    self.assertIn(reference, resolution["resolved"])
                    self.assertEqual(
                        image["digest"],
                        resolution["resolved"][reference]["digest"],
                        "chart digest differs from the source lock",
                    )
                else:
                    self.assertIn(reference, resolution["unresolved"])

        # The only locked images omitted by this first chart skeleton are the
        # documented monitoring, init/helper, and HAProxy components.
        expected_omissions = {
            "alpine:3.23.4",
            "busybox:1.37.0",
            "ghcr.io/google/cadvisor:0.56.2",
            "grafana/grafana:13.0.1-ubuntu",
            "haproxy:3.0-alpine",
            "nvidia/dcgm-exporter:3.3.6-3.4.2-ubuntu22.04",
            "quay.io/prometheus/node-exporter:v1.11.1",
            "quay.io/prometheus/prometheus:v3.11.3",
        }
        self.assertEqual(locked_images - chart_images, expected_omissions)

        source_config = self.template_text["source-configmap.yaml"]
        self.assertIn(".Values.global.source.tag", source_config)
        self.assertIn(".Values.global.source.commit", source_config)
        self.assertIn(".Values.global.source.releaseMetadata", source_config)

    def test_source_lock_digest_resolution_is_complete_and_disjoint(self):
        resolution = self.source_lock["image_digest_resolution"]
        resolved = resolution["resolved"]
        unresolved = resolution["unresolved"]
        locked_images = set(self.source_lock["images"])
        resolved_images = set(resolved)
        unresolved_images = set(unresolved)

        self.assertEqual(resolution["platform"], "linux/amd64")
        self.assertEqual(resolution["resolved_count"], 29)
        self.assertEqual(resolution["unresolved_count"], 0)
        self.assertEqual(len(resolved_images), 29)
        self.assertEqual(len(unresolved_images), 0)
        self.assertFalse(resolved_images & unresolved_images)
        self.assertEqual(resolved_images | unresolved_images, locked_images)

        digest_pattern = re.compile(r"sha256:[a-f0-9]{64}")
        for image, evidence in resolved.items():
            with self.subTest(image=image):
                self.assertIsNotNone(digest_pattern.fullmatch(evidence["digest"]))
                self.assertIsNotNone(digest_pattern.fullmatch(evidence["platform_digest"]))


if __name__ == "__main__":
    unittest.main()
