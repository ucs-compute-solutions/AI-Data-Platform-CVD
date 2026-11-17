"""Offline asset-population contracts; no cluster or GPU work is executed."""

from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import populate_assets as assets


def fixture_plan():
    return {"planSha256": "a" * 64, "sourceMedia": {"seconds": 600, "subPath": "originals"}, "site": {
        "namespace": "warehouse-test", "cluster": {"user": "operator", "server": "https://api.cluster.example.net:6443", "context": "warehouse"},
        "placement": {"cpuNodeSelector": {"node-role.example.net/warehouse-cpu": "true"}},
        "auth": {"imagePullSecrets": ["ngc-pull"]}}}


def fixture_manifests():
    return {"items": [{"kind": "PersistentVolumeClaim", "metadata": {"name": name},
        "spec": {"storageClassName": "vast-csi", "accessModes": ["ReadWriteOnce" if name == assets.MODEL_CLAIM else "ReadWriteMany"]}}
        for name in (assets.MODEL_CLAIM, assets.MEDIA_CLAIM)]}


class PopulationTests(unittest.TestCase):
    def test_pinned_model_and_helper_scope(self):
        self.assertEqual(assets.MODEL_SHA, "0a22264542514149bead6e8582499d9758d51e3fde2892d9d2cc378a60426267")
        pod = assets.helper_pod(fixture_plan(), "warehouse-assets-test", "run-1")
        self.assertEqual(pod["spec"]["serviceAccountName"], "warehouse-gpu")
        self.assertEqual(pod["spec"]["securityContext"]["runAsUser"], 1000)
        self.assertEqual(pod["spec"]["securityContext"]["fsGroup"], 2000)
        self.assertEqual(pod["spec"]["containers"][0]["image"], assets.HELPER_IMAGE)
        self.assertEqual({row["persistentVolumeClaim"]["claimName"] for row in pod["spec"]["volumes"]}, {assets.MODEL_CLAIM, assets.MEDIA_CLAIM})
        self.assertNotIn("nvidia.com/gpu", json.dumps(pod))

    def test_consumer_check_includes_stopped_failed_and_terminating_pods(self):
        def pod(name, uid):
            return {"metadata": {"name": name, "uid": uid}, "status": {"phase": "Succeeded"},
                "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": assets.MODEL_CLAIM}}]}}
        self.assertEqual(assets.consumers([pod("other", "old"), pod("helper", "mine")], "mine"), ["other"])

    def test_plan_does_not_contact_cluster_or_write_outputs(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(assets.deploy, "oc", side_effect=AssertionError("offline plan")):
            receipt, manifest = Path(tmp) / "receipt.json", Path(tmp) / "media.json"
            result = assets.populate(fixture_plan(), fixture_manifests(), [], {"secondsPerCamera": 600, "videos": []},
                execute=False, approved=None, receipt=receipt, manifest_output=manifest)
            self.assertEqual(result["status"], "plan-only")
            self.assertFalse(receipt.exists())
            self.assertFalse(manifest.exists())

    def test_wrong_approval_and_existing_receipt_stop_before_cluster(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(assets.deploy, "oc", side_effect=AssertionError("no mutation")):
            receipt, manifest = Path(tmp) / "receipt.json", Path(tmp) / "media.json"
            with self.assertRaises(assets.deploy.Invalid):
                assets.populate(fixture_plan(), fixture_manifests(), [], {}, execute=True, approved="b" * 64,
                                receipt=receipt, manifest_output=manifest)
            receipt.write_text("retained")
            with self.assertRaises(assets.deploy.Invalid):
                assets.populate(fixture_plan(), fixture_manifests(), [], {}, execute=True, approved="a" * 64,
                                receipt=receipt, manifest_output=manifest)
            self.assertEqual(receipt.read_text(), "retained")

    def test_existing_nonmatching_local_manifest_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "media.json"
            manifest.write_text('{"existing":"data"}')
            with self.assertRaises(assets.deploy.Invalid):
                assets.populate(fixture_plan(), fixture_manifests(), [], {}, execute=False, approved=None,
                                receipt=Path(tmp) / "receipt.json", manifest_output=manifest)
            self.assertEqual(json.loads(manifest.read_text()), {"existing": "data"})

    def test_remote_inspection_matches_absent_and_rejects_mismatch_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"verified asset"
            (root / "asset").write_bytes(data)
            checksum = hashlib.sha256(data).hexdigest()
            def check(relative, digest=checksum):
                return subprocess.run(["/bin/sh", "-ec", assets.PATH_CHECK, "--", str(root), relative, digest, str(len(data))], text=True, capture_output=True)
            self.assertEqual(check("asset").stdout.strip(), "MATCH")
            self.assertEqual(check("absent").stdout.strip(), "ABSENT")
            self.assertNotEqual(check("asset", "b" * 64).returncode, 0)
            (root / "link").symlink_to(root / "asset")
            self.assertNotEqual(check("link").returncode, 0)

    def test_atomic_promotion_never_overwrites_and_keeps_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            partial, target = root / "owned.partial", root / "target"
            content = b"verified asset"
            partial.write_bytes(content)
            checksum = hashlib.sha256(content).hexdigest()
            command = ["/bin/sh", "-ec", assets.PROMOTE, "--", str(partial), str(target), checksum, str(len(content))]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertTrue(partial.exists())
            target.unlink()  # Test fixture only; the implementation has no file removal.
            target.write_bytes(b"unrelated existing data")
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual(target.read_bytes(), b"unrelated existing data")
            self.assertEqual(partial.read_bytes(), content)

    def test_partial_parent_preparation_rejects_an_existing_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            partial, parent = root / "existing", root / "must-not-be-created"
            partial.write_bytes(b"retained")
            result = subprocess.run(["/bin/sh", "-ec", assets.PREPARE_PARTIAL, "--", str(partial), str(parent)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(parent.exists())
            self.assertEqual(partial.read_bytes(), b"retained")

    def test_owned_pod_check_fails_before_any_action_on_replacement(self):
        pod = {"metadata": {"uid": "foreign", "labels": {assets.RUN_LABEL: "run"}}}
        with patch.object(assets.deploy, "oc", return_value=json.dumps(pod)), patch.object(assets.subprocess, "Popen") as popen:
            with self.assertRaises(assets.deploy.Invalid):
                assets.delete_owned_helper("namespace", "pod", "expected", "run")
            popen.assert_not_called()

    def test_cleanup_deletes_only_uid_bound_helper_through_loopback_proxy(self):
        pod = {"metadata": {"uid": "my-uid", "labels": {assets.RUN_LABEL: "run-1"}}}
        class Process:
            stdout = io.StringIO("Starting to serve on 127.0.0.1:12345\n")
            def poll(self): return None
            def terminate(self): pass
            def wait(self, timeout): return 0
        class Response:
            status = 200
            def read(self, limit): return b"{}"
        class Connection:
            call = None
            def __init__(self, host, port, timeout): self.host, self.port = host, port
            def request(self, method, path, **kwargs): Connection.call = (self.host, self.port, method, path, kwargs)
            def getresponse(self): return Response()
            def close(self): pass
        with patch.object(assets.deploy, "oc", side_effect=[json.dumps(pod), ""]), \
                patch.object(assets.subprocess, "Popen", return_value=Process()), patch.object(assets.http.client, "HTTPConnection", Connection):
            assets.delete_owned_helper("namespace", "owned-helper", "my-uid", "run-1")
        host, port, method, path, kwargs = Connection.call
        self.assertEqual((host, port, method, path), ("127.0.0.1", 12345, "DELETE", "/api/v1/namespaces/namespace/pods/owned-helper"))
        self.assertEqual(json.loads(kwargs["body"])["preconditions"], {"uid": "my-uid"})
        self.assertNotIn("persistentvolume", path)

    def execute_fixture(self, tmp, *, copy_error=None, cleanup_error=None):
        plan, manifests = fixture_plan(), fixture_manifests()
        def oc(arguments, **kwargs):
            if arguments == ["whoami"]: return plan["site"]["cluster"]["user"]
            if arguments == ["whoami", "--show-server"]: return plan["site"]["cluster"]["server"]
            if arguments == ["config", "current-context"]: return plan["site"]["cluster"]["context"]
            if "pvc" in arguments:
                name = arguments[arguments.index("pvc") + 1]
                if "jsonpath=" in arguments[-1]: return name + "-uid"
                spec = next(row["spec"] for row in manifests["items"] if row["metadata"]["name"] == name)
                return json.dumps({"metadata": {"uid": name + "-uid"}, "status": {"phase": "Bound"}, "spec": spec})
            if arguments[:2] == ["create", "--dry-run=server"]: return "pod/helper"
            if arguments[:2] == ["create", "-f"]: return '{"metadata":{"uid":"owned-uid"}}'
            if "wait" in arguments: return "Ready"
            raise AssertionError(arguments)
        receipt, media = Path(tmp) / "assets.json", Path(tmp) / "media.json"
        with patch.object(assets.deploy, "oc", side_effect=oc), patch.object(assets, "no_consumers"), \
                patch.object(assets, "copy_assets", side_effect=copy_error, return_value=[{"status": "copied-verified"}]), \
                patch.object(assets, "delete_owned_helper", side_effect=cleanup_error) as delete:
            try:
                result = assets.populate(plan, manifests, [], {"secondsPerCamera": 600, "videos": []}, execute=True,
                    approved=plan["planSha256"], receipt=receipt, manifest_output=media)
            except assets.deploy.Invalid:
                result = None
        return result, json.loads(receipt.read_text()), media, delete

    def test_success_requires_remote_hashes_and_owned_helper_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, receipt, media, delete = self.execute_fixture(tmp)
            self.assertEqual(result["status"], "pass")
            self.assertTrue(receipt["pvcHashesVerified"])
            self.assertTrue(receipt["helperPodRemoved"])
            self.assertTrue(media.is_file())
            delete.assert_called_once()

    def test_copy_failure_retains_failed_receipt_and_still_cleans_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, receipt, media, delete = self.execute_fixture(tmp, copy_error=assets.deploy.Invalid("copy failed"))
            self.assertIsNone(result)
            self.assertEqual(receipt["status"], "failed")
            self.assertFalse(receipt["pvcHashesVerified"])
            self.assertTrue(receipt["helperPodRemoved"])
            self.assertFalse(media.exists())
            delete.assert_called_once()

    def test_cleanup_failure_never_publishes_pass_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, receipt, media, _delete = self.execute_fixture(tmp, cleanup_error=assets.deploy.Invalid("UID mismatch"))
            self.assertIsNone(result)
            self.assertEqual(receipt["status"], "failed-helper-cleanup")
            self.assertFalse(receipt["helperPodRemoved"])
            self.assertFalse(media.exists())

    def test_invalid_model_is_rejected_before_video_tools(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(assets, "media_tools", side_effect=AssertionError("no probes")):
            model = Path(tmp) / "model.onnx"
            model.write_bytes(b"wrong model")
            with self.assertRaises(assets.deploy.Invalid):
                assets.local_assets(fixture_plan(), model, Path(tmp), hour_manifest=None, ffprobe="ffprobe", ffmpeg="ffmpeg")

    def test_600s_sources_require_four_hashes_and_actual_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            model = directory / "model.onnx"
            model.write_bytes(b"model fixture")
            tools = assets.media_tools()
            sources = {}
            for filename in tools.SOURCES:
                path = directory / filename
                path.write_bytes(filename.encode())
                sources[filename] = assets.sha256(path)
            metadata = {"streams": [{"codec_type": "video", "codec_name": "h264", "avg_frame_rate": "30/1", "width": 1920,
                "height": 1080, "nb_read_packets": "18000"}], "format": {"duration": "600.0"}}
            with patch.object(assets, "MODEL_SHA", assets.sha256(model)), patch.object(assets, "media_tools", return_value=tools), \
                    patch.object(tools, "SOURCES", sources), patch.object(tools, "probe", return_value=metadata) as probe:
                files, manifest = assets.local_assets(fixture_plan(), model, directory, hour_manifest=None, ffprobe="ffprobe", ffmpeg="ffmpeg")
            self.assertEqual(len(files), 5)
            self.assertEqual(len(manifest["videos"]), 4)
            self.assertEqual(probe.call_count, 4)
            self.assertTrue(all(row["seconds"] == 600 for row in manifest["videos"]))

    def test_hour_sources_require_preparation_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "model.onnx"
            model.write_bytes(b"model fixture")
            plan = fixture_plan()
            plan["sourceMedia"]["seconds"] = 3600
            with patch.object(assets, "MODEL_SHA", assets.sha256(model)):
                with self.assertRaises(assets.deploy.Invalid):
                    assets.local_assets(plan, model, Path(tmp), hour_manifest=None, ffprobe="ffprobe", ffmpeg="ffmpeg")

    def test_hour_sources_revalidate_each_file_and_repeat_joins(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            model = directory / "model.onnx"
            model.write_bytes(b"model fixture")
            plan = fixture_plan()
            plan["sourceMedia"]["seconds"] = 3600
            tools = assets.media_tools()
            sources, videos = {}, []
            for filename in tools.SOURCES:
                path = directory / filename
                path.write_bytes(filename.encode())
                sources[filename] = hashlib.sha256(("original-" + filename).encode()).hexdigest()
                videos.append({"filename": filename, "sourceSha256": sources[filename], "sha256": assets.sha256(path), "bytes": path.stat().st_size})
            hour = directory / "manifest.json"
            hour.write_text(json.dumps({"kind": "WarehouseDerivedHourVideos", "secondsPerCamera": 3600,
                "repeatedOriginalPasses": 6, "audio": False, "videos": videos}))
            raw = {"streams": [{"codec_type": "video", "codec_name": "h264", "avg_frame_rate": "30/1", "width": 1920,
                "height": 1080, "nb_read_packets": "108000"}], "format": {"duration": "3600.0"}}
            metadata = {"seconds": 3600, "fps": 30, "codec": "h264", "packets": 108000, "width": 1920, "height": 1080}
            with patch.object(assets, "MODEL_SHA", assets.sha256(model)), patch.object(assets, "media_tools", return_value=tools), \
                    patch.object(tools, "SOURCES", sources), patch.object(tools, "probe", return_value=raw), \
                    patch.object(tools, "validate_media", return_value=metadata) as validate:
                _files, manifest = assets.local_assets(plan, model, directory, hour_manifest=hour, ffprobe="ffprobe", ffmpeg="ffmpeg")
            self.assertEqual(validate.call_count, 4)
            self.assertTrue(manifest["repeatJoinsRevalidated"])
            self.assertEqual(manifest["repeatedOriginalPasses"], 6)
            self.assertTrue(all(call.args[-1]["packets"] == 18000 for call in validate.call_args_list))


if __name__ == "__main__":
    unittest.main()
