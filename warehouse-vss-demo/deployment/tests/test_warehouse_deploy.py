"""Offline contracts for new-site installation planning; no oc is executed."""
import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("warehouse_deploy", ROOT / "warehouse_deploy.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
IMAGE = "registry.example.org/product/component@sha256:" + "a" * 64


def fixture():
    site = json.loads((ROOT / "site.example.json").read_text())
    resources, components = [], {}
    for role in deploy.REQUIRED_ROLES:
        name = "warehouse-" + role.lower()
        component = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": "@@NAMESPACE@@"},
                     "spec": {"replicas": 0 if role == "nvstreamer" else 1,
                              "selector": {"matchLabels": {"app": name}},
                              "template": {"metadata": {"labels": {"app": name}}, "spec": {
                                  "containers": [{"name": role, "image": IMAGE, "resources": {
                                      "limits": {"nvidia.com/gpu": 1} if role in deploy.GPU_ROLES else {}}}],
                                  "volumes": [{"name": "config", "configMap": {"name": "warehouse-config"}}]}}}}
        components[role] = deploy.reference_key(component)
        resources.append(component)
    resources += [{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "warehouse-config", "namespace": "@@NAMESPACE@@"},
                   "data": {"url": "https://@@PUBLIC_HOST@@", "service": "warehouse-vios.@@NAMESPACE@@.svc.@@CLUSTER_DOMAIN@@"}},
                  {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "warehouse-data", "namespace": "@@NAMESPACE@@"},
                   "spec": {"accessModes": ["ReadWriteMany"], "resources": {"requests": {"storage": "1Gi"}}}}]
    baseline = {"format": 1, "kind": "warehouse-runtime-template", "status": "reviewed-accepted-runtime",
                "secretReviewComplete": True, "corrections": {name: {"status": "verified", "evidenceSha256": "b" * 64} for name in deploy.CORRECTIONS},
                "bootstrap": {"reviewed": True}, "components": components, "resources": resources,
                "forbiddenSourceStrings": ["oldsite.example.invalid"]}
    return baseline, site


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.baseline, self.site = fixture()

    def compile(self):
        return deploy.compile_resources(self.baseline, self.site)

    def test_new_site_namespaces_storage_images_and_five_gpu_roles(self):
        before = copy.deepcopy(self.baseline)
        result = self.compile()
        self.assertEqual(self.baseline, before)
        self.assertTrue(all(item["metadata"]["namespace"] == "warehouse-vss" for item in result))
        self.assertNotIn("@@", json.dumps(result))
        claim = next(item for item in result if item["kind"] == "PersistentVolumeClaim")
        self.assertEqual(claim["spec"]["storageClassName"], "vast-rwx")
        self.assertEqual(sum(int(c.get("resources", {}).get("limits", {}).get("nvidia.com/gpu", 0)) for item in result for c in (deploy.pod_spec(item) or {}).get("containers", [])), 5)
        source = next(item for item in result if deploy.reference_key(item) == self.baseline["components"]["nvstreamer"])
        self.assertEqual(source["spec"]["replicas"], 0)

    def test_captured_baseline_binds_all_service_fqdns_to_target_domain(self):
        baseline = json.loads((ROOT / "baseline/baseline.captured.json").read_text())
        site = json.loads((ROOT / "baseline/site.render-example.json").read_text())
        site["cluster"]["domain"] = "cluster.example"
        encoded = json.dumps(deploy.compile_resources(baseline, site, bootstrap=True))
        self.assertNotIn(".svc.cluster.local", encoded)
        self.assertIn("warehouse-nvstreamer.warehouse-vss.svc.cluster.example:31554", encoded)
        self.assertIn("warehouse-vios-streamprocessing.warehouse-vss.svc.cluster.example", encoded)

    def test_hardcoded_service_domain_requires_capture_correction(self):
        self.baseline["resources"][-2]["data"]["service"] = "warehouse-vios.warehouse.svc.cluster.local"
        with self.assertRaisesRegex(deploy.Invalid, "Captured service DNS"):
            self.compile()

    def test_pending_baseline_lists_exact_gaps_and_refuses_render(self):
        baseline = {"format": 1, "kind": "warehouse-runtime-template",
                    "status": "awaiting-accepted-runtime-reconstruction",
                    "secretReviewComplete": False, "components": {},
                    "corrections": {}, "bootstrap": {"reviewed": False},
                    "forbiddenSourceStrings": [], "resources": []}
        gaps = deploy.baseline_gaps(baseline)
        self.assertGreater(len(gaps), 20)
        with self.assertRaisesRegex(deploy.Invalid, "Baseline is incomplete"):
            deploy.compile_resources(baseline, self.site)

    def test_missing_correction_evidence_is_not_waived(self):
        self.baseline["corrections"][deploy.CORRECTIONS[0]]["status"] = "missing"
        with self.assertRaisesRegex(deploy.Invalid, "Missing accepted correction"):
            self.compile()

    def test_redacted_runtime_fields_are_never_defaults(self):
        self.baseline["resources"][0]["spec"]["template"]["spec"]["automountServiceAccountToken"] = "[REDACTED]"
        with self.assertRaisesRegex(deploy.Invalid, "Redacted runtime"):
            self.compile()

    def test_secret_object_and_literal_environment_are_rejected(self):
        for secret in (True, False):
            baseline, site = fixture()
            if secret:
                baseline["resources"].append({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "do-not-copy"}})
            else:
                baseline["resources"][0]["spec"]["template"]["spec"]["containers"][0]["env"] = [{"name": "NGC_API_KEY", "value": "not-a-real-key"}]
            with self.assertRaisesRegex(deploy.Invalid, "Secret objects|Literal credential"):
                deploy.compile_resources(baseline, site)

    def test_literal_config_password_is_rejected(self):
        self.baseline["resources"][-2]["data"]["configuration.json"] = '{"password": "not-a-real-password"}'
        with self.assertRaisesRegex(deploy.Invalid, "Possible literal credential"):
            self.compile()

    def test_secret_reference_is_mapped_without_values(self):
        self.baseline["resources"][0]["spec"]["template"]["spec"]["containers"][0]["env"] = [{"name": "NGC_API_KEY", "valueFrom": {"secretKeyRef": {"name": "warehouse-ngc-api", "key": "api-key"}}}]
        result = self.compile()
        source = next(item for item in result if deploy.reference_key(item) == self.baseline["components"]["nvstreamer"])
        self.assertEqual(source["spec"]["template"]["spec"]["containers"][0]["env"][0]["valueFrom"]["secretKeyRef"], {"name": "ngc-api", "key": "api-key"})

    def test_unmapped_secret_ref_is_rejected(self):
        self.baseline["resources"][0]["spec"]["template"]["spec"]["volumes"].append({"name": "auth", "secret": {"secretName": "unknown"}})
        with self.assertRaisesRegex(deploy.Invalid, "Unmapped Secret"):
            self.compile()

    def test_foreign_namespace_server_state_and_node_pin_rejected(self):
        for field in ("namespace", "resourceVersion", "nodeName"):
            baseline, site = fixture()
            if field == "namespace": baseline["resources"][0]["metadata"][field] = "other"
            elif field == "nodeName": baseline["resources"][0]["spec"]["template"]["spec"][field] = "old-worker"
            else: baseline["resources"][0]["metadata"][field] = "1234"
            with self.assertRaises(deploy.Invalid): deploy.compile_resources(baseline, site)

    def test_tag_only_images_and_unknown_tokens_rejected(self):
        for image in ("registry.example.org/model:1", "@@UNREVIEWED@@/model@sha256:" + "a" * 64):
            baseline, site = fixture()
            baseline["resources"][0]["spec"]["template"]["spec"]["containers"][0]["image"] = image
            with self.assertRaises(deploy.Invalid): deploy.compile_resources(baseline, site)

    def test_source_cannot_start_as_install_side_effect(self):
        self.baseline["resources"][0]["spec"]["replicas"] = 1
        with self.assertRaisesRegex(deploy.Invalid, "NvStreamer stopped"):
            self.compile()

    def test_duplicate_and_unresolved_configmap_rejected(self):
        self.baseline["resources"].append(copy.deepcopy(self.baseline["resources"][-1]))
        with self.assertRaisesRegex(deploy.Invalid, "Duplicate"):
            self.compile()
        self.baseline, _ = fixture()
        self.baseline["resources"] = [item for item in self.baseline["resources"] if item["kind"] != "ConfigMap"]
        with self.assertRaisesRegex(deploy.Invalid, "Unresolved ConfigMap"):
            self.compile()

    def test_repository_mirror_preserves_digest(self):
        self.site["images"]["repositoryMirrors"] = {"registry.example.org/product/component": "mirror.example.net/warehouse/component"}
        result = self.compile()
        image = next(item for item in result if deploy.pod_spec(item))["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual(image, "mirror.example.net/warehouse/component@sha256:" + "a" * 64)

    def test_repository_mirror_rejects_empty_or_tagged_repository_before_render(self):
        for target in ("", "mirror.example.net/component:latest", "https://mirror.example.net/component"):
            self.site["images"]["repositoryMirrors"] = {"registry.example.org/product/component": target}
            with self.assertRaisesRegex(deploy.Invalid, "nonempty repositories"):
                deploy.validate_site(self.site)

    def test_rebuilt_image_requires_an_explicit_reviewed_full_pin_mapping(self):
        target = "rebuilt.example.net/warehouse/component@sha256:" + "c" * 64
        self.site["images"]["reviewedImageOverrides"] = {IMAGE: target}
        result = self.compile()
        self.assertTrue(all(container["image"] == target for item in result for container in (deploy.pod_spec(item) or {}).get("containers", [])))
        self.site["images"]["reviewedImageOverrides"] = {IMAGE: "rebuilt.example.net/component:current"}
        with self.assertRaisesRegex(deploy.Invalid, "two full sha256 pins"):
            self.compile()

    def test_no_jobs_nodeports_pv_binding_or_unknown_site_fields(self):
        self.site["auth"]["password"] = "must-not-be-accepted"
        with self.assertRaisesRegex(deploy.Invalid, "references only"):
            self.compile()
        self.baseline, self.site = fixture()
        self.baseline["resources"][-1]["spec"]["volumeName"] = "retained-pv"
        with self.assertRaisesRegex(deploy.Invalid, "retained PV"):
            self.compile()

    def test_render_plan_tamper_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "review"
            plan = deploy.render(self.baseline, self.site, output)
            self.assertEqual(deploy.load_plan(output)[0], plan)
            with self.assertRaisesRegex(deploy.Invalid, "already exists"):
                deploy.render(self.baseline, self.site, output)
            path = output / "manifests.json"
            path.write_text(path.read_text().replace("vast-rwx", "other-storage"))
            with self.assertRaisesRegex(deploy.Invalid, "changed"):
                deploy.load_plan(output)

    def test_apply_requires_reviewed_sha_before_any_oc_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "review"
            deploy.render(self.baseline, self.site, output)
            with mock.patch.object(deploy, "oc") as client:
                with self.assertRaisesRegex(deploy.Invalid, "exact reviewed"):
                    deploy.review_or_apply(output, execute=True, approved="wrong")
                client.assert_not_called()

    def mock_oc(self, args, **kwargs):
        if args == ["whoami"]: return self.site["cluster"]["user"]
        if args == ["whoami", "--show-server"]: return self.site["cluster"]["server"]
        if args == ["config", "current-context"]: return self.site["cluster"]["context"]
        if args[:2] == ["get", "namespace"]: return "namespace/" + self.site["namespace"]
        if "jsonpath={.metadata.name}" in args: return args[4]
        if "jsonpath={.metadata.uid}" in args: return "fixture-uid-" + args[4]
        if "deployment,statefulset,daemonset" in args:
            resources, _ = deploy.staged(deploy.compile_resources(self.baseline, self.site))
            controllers = [item for item in resources if item["kind"] in {"Deployment", "StatefulSet", "DaemonSet"}]
            for item in controllers: item["metadata"]["uid"] = "fixture-uid-" + item["metadata"]["name"]
            return json.dumps({"items": controllers})
        if args[-2:] == ["-o", "json"]: return '{"items": []}'
        if args[0] == "create": return "deployment.apps/example"
        return ""

    def test_dry_run_is_not_create_and_secrets_are_metadata_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "review"
            deploy.render(self.baseline, self.site, output)
            with mock.patch.object(deploy, "oc", side_effect=self.mock_oc) as client:
                result = deploy.review_or_apply(output)
            self.assertEqual(result["status"], "dry-run-pass")
            calls = [call.args[0] for call in client.call_args_list]
            self.assertTrue(all("--dry-run=server" in args for args in calls if args[0] == "create"))
            self.assertTrue(all(args[-1] == "jsonpath={.metadata.name}" for args in calls if "secret" in args))

    def test_identity_mismatch_prevents_server_dry_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "review"
            deploy.render(self.baseline, self.site, output)
            with mock.patch.object(deploy, "oc", return_value="different-user") as client:
                with self.assertRaisesRegex(deploy.Invalid, "identity"):
                    deploy.review_or_apply(output)
                self.assertEqual(client.call_count, 1)

    def test_execute_only_creates_reviewed_resources_never_deletes(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "review"
            plan = deploy.render(self.baseline, self.site, output)
            with mock.patch.object(deploy, "oc", side_effect=self.mock_oc) as client:
                result = deploy.review_or_apply(output, True, plan["planSha256"])
            self.assertFalse(result["freshInstallAccepted"])
            calls = [call.args[0] for call in client.call_args_list]
            self.assertEqual(sum(args[0] == "create" and "--dry-run=server" not in args for args in calls), 1)
            self.assertTrue(all(not set(args) & {"delete", "apply", "replace", "--force", "--prune"} for args in calls))

    def add_job(self):
        self.baseline["bootstrap"]["jobs"] = [{"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "warehouse-initialize-fixture", "namespace": "@@NAMESPACE@@"},
            "spec": {"backoffLimit": 1, "activeDeadlineSeconds": 120, "template": {"metadata": {}, "spec": {"restartPolicy": "Never", "containers": [{"name": "initialize", "image": IMAGE}]}}}}]

    def test_render_stops_applications_and_keeps_separate_initialization_jobs(self):
        self.add_job()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "review"
            plan = deploy.render(self.baseline, self.site, output)
            manifests = deploy.read_json(output / "manifests.json")
            jobs = deploy.read_json(output / "initialization-jobs.json")
            self.assertTrue(all(item["spec"]["replicas"] == 0 for item in manifests["items"] if item["kind"] == "Deployment"))
            self.assertEqual(len(jobs["items"]), 1)
            self.assertTrue(all(item["kind"] != "Job" for item in manifests["items"]))
            self.assertEqual(plan["sourceMedia"], {"seconds": 3600, "subPath": "hour"})

    def test_initialization_and_activation_are_explicit_and_receipt_bound(self):
        self.add_job()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "review"
            plan = deploy.render(self.baseline, self.site, output)
            with mock.patch.object(deploy, "oc", side_effect=self.mock_oc):
                deploy.review_or_apply(output, True, plan["planSha256"])
                result = deploy.initialize(output)
                self.assertEqual(result["status"], "initialization-dry-run-pass")
                self.assertFalse((output / "initialization-receipt.json").exists())
                result = deploy.initialize(output, True, plan["planSha256"])
                self.assertEqual(result["status"], "pass")
                with self.assertRaises(FileNotFoundError): deploy.activate(output)
                deploy.write_json(output / "assets-receipt.json", {"status": "pass", "planSha256": plan["planSha256"],
                    "modelSha256": "0a22264542514149bead6e8582499d9758d51e3fde2892d9d2cc378a60426267", "pvcHashesVerified": True, "helperPodRemoved": True,
                    "mediaManifest": {"secondsPerCamera": 3600}, "claimUids": {name: "fixture-uid-" + name for name in ("warehouse-source-videos", "warehouse-perception-models")}})
                result = deploy.activate(output)
                self.assertTrue(result["sourceRemainsStopped"])
                result = deploy.activate(output, True, plan["planSha256"])
                self.assertFalse(result["freshInstallAccepted"])

    def test_preview_never_creates_an_installable_plan(self):
        self.baseline["status"] = "captured-unreviewed"
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "preview"
            result = deploy.preview(self.baseline, self.site, output)
            self.assertEqual(result["status"], "not-installable")
            self.assertFalse((output / "plan.json").exists())


if __name__ == "__main__":
    unittest.main()
