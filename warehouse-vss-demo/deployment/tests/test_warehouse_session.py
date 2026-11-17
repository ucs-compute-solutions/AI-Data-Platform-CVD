"""Native API contract tests with newly generated camera IDs, no cluster."""
import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import warehouse_session as session
import warehouse_deploy as deploy

SOURCE = "warehouse-nvstreamer.demo.svc.cluster.local"
VIOS = "warehouse-vios-streamprocessing.demo.svc.cluster.local"


class NativeFixture:
    def __init__(self):
        self.cameras = {}
        self.registered = set()
        self.calls = []
        self.lock = threading.Lock()
        self.bad_proxy = False

    def api(self, service):
        fixture = self
        class Client:
            def call(self, method, path, data=None):
                with fixture.lock:
                    fixture.calls.append((service, method, path, copy.deepcopy(data)))
                    if service == "source":
                        return [{"source-" + str(i): [{"name": name, "url": f"rtsp://source-pod.example.org:{31554+i}/nvstream/tmp/nv_streamer/videos/{name}.mp4"}]}
                                for i, name in enumerate(session.CAMERAS)]
                    if service == "sensor":
                        if method == "POST":
                            ident = "new-native-id-" + str(len(fixture.cameras) + 1)
                            fixture.cameras[data["name"]] = {"name": data["name"], "sensorId": ident, "sensorIp": SOURCE}
                            return {"sensorId": ident}
                        return copy.deepcopy(list(fixture.cameras.values()))
                    if path == "/proxy/streams":
                        return [{"sensorId": item["sensorId"], "proxyUrl": f"rtsp://{'wrong.example.org' if fixture.bad_proxy else VIOS}:30554/live/{item['sensorId']}"}
                                for item in fixture.cameras.values()]
                    if path == "/record/streams": return [{ident: [{"url": f"rtsp://{VIOS}:30554/live/{ident}"}]} for ident in fixture.registered]
                    if path == "/record/stream/add":
                        fixture.registered.add(data["id"])
                        return {}
                    if path == "/record/status": return {item["sensorId"]: {"recording_status": "off"} for item in fixture.cameras.values()}
                    raise AssertionError((service, method, path))
        return Client()


class CameraTests(unittest.TestCase):
    def test_default_plan_is_read_only(self):
        native = NativeFixture()
        result = session.bootstrap(native.api("source"), native.api("sensor"), native.api("recorder"), SOURCE, VIOS)
        self.assertEqual(result["missingCameraNames"], list(session.CAMERAS))
        self.assertFalse(any(method != "GET" for _, method, _, _ in native.calls))

    def test_fresh_ids_are_native_discovered_and_recorders_are_added_without_deletes(self):
        native = NativeFixture()
        result = session.bootstrap(native.api("source"), native.api("sensor"), native.api("recorder"), SOURCE, VIOS, True)
        self.assertEqual(set(result["cameras"]), set(session.CAMERAS))
        self.assertEqual(set(result["cameras"].values()), native.registered)
        self.assertEqual(len(native.registered), 4)
        adds = [data for service, method, path, data in native.calls if service == "sensor" and method == "POST"]
        self.assertTrue(all(SOURCE in data["sensorUrl"] and "transport=tcp" in data["sensorUrl"] for data in adds))
        self.assertTrue(all(data["username"] == data["password"] == "" for data in adds))
        self.assertFalse(any(method == "DELETE" for _, method, _, _ in native.calls))

    def test_warm_replay_keeps_ids_and_does_not_duplicate_registrations(self):
        native = NativeFixture()
        first = session.bootstrap(native.api("source"), native.api("sensor"), native.api("recorder"), SOURCE, VIOS, True)
        native.calls.clear()
        second = session.bootstrap(native.api("source"), native.api("sensor"), native.api("recorder"), SOURCE, VIOS, True)
        self.assertEqual(first["cameras"], second["cameras"])
        self.assertFalse(any(method == "POST" for _, method, _, _ in native.calls))

    def test_wrong_proxy_host_is_not_accepted_or_repaired_by_deletion(self):
        native = NativeFixture()
        native.bad_proxy = True
        with self.assertRaisesRegex(deploy.Invalid, "stable Service"):
            session.bootstrap(native.api("source"), native.api("sensor"), native.api("recorder"), SOURCE, VIOS, True)
        self.assertFalse(any(method == "DELETE" for _, method, _, _ in native.calls))

    def test_unknown_camera_in_existing_inventory_stops_without_writes(self):
        native = NativeFixture()
        native.cameras["Unexpected"] = {"name": "Unexpected", "sensorId": "foreign-camera", "sensorIp": SOURCE}
        with self.assertRaisesRegex(deploy.Invalid, "extra, duplicate"):
            session.bootstrap(native.api("source"), native.api("sensor"), native.api("recorder"), SOURCE, VIOS, True)
        self.assertFalse(any(method != "GET" for _, method, _, _ in native.calls))

    def test_retained_wrong_source_does_not_recreate_camera(self):
        native = NativeFixture()
        native.cameras["Camera"] = {"name": "Camera", "sensorId": "retained-id", "sensorIp": "other.example.org"}
        with self.assertRaisesRegex(deploy.Invalid, "preserve-ID migration"):
            session.bootstrap(native.api("source"), native.api("sensor"), native.api("recorder"), SOURCE, VIOS, True)
        self.assertFalse(any(method != "GET" for _, method, _, _ in native.calls))

    def test_session_plan_never_calls_oc_or_starts_gpu_source(self):
        site = json.loads((ROOT / "site.example.json").read_text())
        with mock.patch.object(deploy, "oc") as client:
            result = session.run_session(site, "registry.example.org/nvstreamer@sha256:" + "a" * 64, 600, False, False, Path("unused.json"), 120)
            self.assertEqual(result["status"], "plan-only")
            self.assertEqual(result["sourceStopDeadlineSecondsAfterScale"], 540)
            self.assertEqual(result["recordSeconds"], 120)
            client.assert_not_called()

    def test_api_will_not_follow_unverified_remote_endpoint(self):
        for base in ("http://server.example.org:80", "https://127.0.0.1:8443", "http://user:password@127.0.0.1:8080"):
            with self.assertRaises(deploy.Invalid): session.API(base)

    def test_timeline_parser_accepts_fractional_precision_on_supported_python(self):
        class Client:
            def call(self, *args): return [{"startTime": "2026-11-16T12:00:00.1Z", "endTime": "2026-11-16T12:00:01.12345Z"}]
        self.assertGreater(session.timeline_end(Client(), "fresh-id"), 0)

    def test_ready_does_not_latch_after_later_media_stall(self):
        previous, advances, last = {}, {"a": 0}, {"a": 0}
        self.assertFalse(session.observe_progress({"a": 10}, previous, advances, last, 0))
        self.assertFalse(session.observe_progress({"a": 20}, previous, advances, last, 20))
        self.assertTrue(session.observe_progress({"a": 30}, previous, advances, last, 40))
        with self.assertRaisesRegex(deploy.Invalid, "timeline stalled"):
            session.observe_progress({"a": 30}, previous, advances, last, 161)

    def test_source_manifest_cannot_be_replaced_by_duration_assertion(self):
        with self.assertRaisesRegex(deploy.Invalid, "verified source-media"):
            session.verify_media_receipt(None, 600)
        manifest = {"secondsPerCamera": 600, "videos": [{"filename": name + ".mp4", "bytes": 1, "sha256": "a" * 64, "seconds": 600} for name in session.CAMERAS]}
        self.assertEqual(session.verify_media_receipt(manifest, 600)["secondsPerCamera"], 600)
        manifest["videos"][0]["seconds"] = 3600
        with self.assertRaisesRegex(deploy.Invalid, "exact duration"):
            session.verify_media_receipt(manifest, 600)

    def test_native_readiness_retries_get_without_mutation(self):
        action = mock.Mock(side_effect=[deploy.Invalid("warming up"), {"ready": True}])
        sleeper = mock.Mock()
        self.assertEqual(session.wait_native(action, sleep=sleeper), {"ready": True})
        self.assertEqual(action.call_count, 2)
        sleeper.assert_called_once_with(2)

    def test_source_cleanup_waits_for_terminating_pods_then_proves_absence(self):
        image = "registry.example.org/source@sha256:" + "a" * 64
        deployment = {"metadata": {"uid": "owned-source"}, "spec": {"replicas": 0,
            "selector": {"matchLabels": {"app": "warehouse-nvstreamer"}},
            "template": {"spec": {"containers": [{"name": "nvstreamer", "image": image}]}}}}
        responses = [deployment, {"items": [{"metadata": {"deletionTimestamp": "2026-11-16T00:00:00Z"}}]},
                     deployment, {"items": []}]
        with mock.patch.object(deploy, "oc", side_effect=[json.dumps(item) for item in responses]) as client:
            sleeper = mock.Mock()
            session.wait_source_stopped("demo", "owned-source", image, sleep=sleeper)
            sleeper.assert_called_once_with(2)
            self.assertEqual(client.call_count, 4)
            self.assertTrue(all("delete" not in call.args[0] for call in client.call_args_list))

    def test_source_cleanup_never_passes_on_remaining_pod_or_changed_identity(self):
        image = "registry.example.org/source@sha256:" + "a" * 64
        deployment = {"metadata": {"uid": "owned-source"}, "spec": {"replicas": 0,
            "selector": {"matchLabels": {"app": "warehouse-nvstreamer"}},
            "template": {"spec": {"containers": [{"name": "nvstreamer", "image": image}]}}}}
        with mock.patch.object(deploy, "oc", side_effect=[json.dumps(deployment), json.dumps({"items": [{"metadata": {"name": "terminating"}}]})]):
            with self.assertRaisesRegex(deploy.Invalid, "phase deadline"):
                session.wait_source_stopped("demo", "owned-source", image, seconds=0)
        deployment["metadata"]["uid"] = "different-source"
        with mock.patch.object(deploy, "oc", return_value=json.dumps(deployment)):
            with self.assertRaisesRegex(deploy.Invalid, "Stopped-source identity"):
                session.wait_source_stopped("demo", "owned-source", image)

    def test_fresh_postgresql_script_is_syntactically_valid_and_not_a_legacy_migration(self):
        path = ROOT / "fresh-postgres-bootstrap.sh"
        subprocess.run(["sh", "-n", str(path)], check=True, capture_output=True)
        text = path.read_text()
        self.assertIn("initdb", text)
        self.assertIn("Refusing an unowned preexisting PGDATA", text)
        self.assertIn("CREATE DATABASE nvcentralizedb OWNER vst", text)
        self.assertNotIn("ALTER DATABASE vios RENAME", text)
        self.assertNotIn("set -x", text)


if __name__ == "__main__":
    unittest.main()
