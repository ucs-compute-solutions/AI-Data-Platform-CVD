"""Offline lifecycle and fail-closed ingestion regressions; no live APIs."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import warehouse_deploy as deploy
import warehouse_session as session
from test_warehouse_session import NativeFixture, SOURCE, VIOS


class Clock:
    def __init__(self): self.now = 0
    def monotonic(self): return self.now
    def sleep(self, seconds): self.now += seconds
    def time(self): return 1_795_002_000 + self.now


class Lifecycle:
    """Exercise the real session with fake native APIs, clock and oc transport."""
    image = "registry.example.org/nvstreamer@sha256:" + "a" * 64

    def __init__(self):
        self.clock, self.native = Clock(), NativeFixture()
        self.site = json.loads((ROOT / "site.example.json").read_text())
        self.site["namespace"] = "demo"
        self.site["cluster"]["domain"] = "cluster.local"
        for key in ("sccName", "clusterRoleName"):
            self.site["security"][key] = "demo-" + key.lower()
        self.replicas, self.active, self.closed, self.oc_calls = 0, set(), [], []
        self.recording_actions, self.oc_timeouts = [], []
        self.timelines_advance = True
        self.media = {"secondsPerCamera": 600, "videos": [
            {"filename": name + ".mp4", "bytes": 1, "sha256": "a" * 64, "seconds": 600}
            for name in session.CAMERAS]}
        self.clients = {}
        owner = self
        for name in ("source", "sensor", "recorder"):
            underlying = self.native.api(name)
            class Client:
                def __init__(self, name, underlying): self.name, self.underlying = name, underlying
                def call(self, method, path, data=None):
                    if self.name == "recorder":
                        if path == "/record/status":
                            return {item["sensorId"]: {"recording_status": "user" if item["sensorId"] in owner.active else "off"}
                                    for item in owner.native.cameras.values()}
                        if path.endswith("/start"):
                            owner.recording_actions.append(("start", path.split("/")[2]))
                            owner.active.add(path.split("/")[2])
                            return {}
                        if path.endswith("/stop"):
                            owner.recording_actions.append(("stop", path.split("/")[2]))
                            owner.active.discard(path.split("/")[2])
                            return {}
                        if path.endswith("/timelines"):
                            if not owner.active: return []
                            epoch = owner.clock.time() if owner.timelines_advance else 1_735_689_600 + owner.clock.now
                            return [{"endTime": datetime.fromtimestamp(epoch, timezone.utc).isoformat()}]
                    return self.underlying.call(method, path, data)
            self.clients[name] = Client(name, underlying)

    def forward(self, namespace, service, port, **kwargs):
        name = next(key for key, value in session.SERVICES.items() if value[0] == service)
        owner = self
        class Forward:
            api = owner.clients[name]
            def close(self): owner.closed.append(name)
        return Forward()

    def oc(self, args, **kwargs):
        self.oc_calls.append(args)
        self.oc_timeouts.append(kwargs.get("timeout"))
        for command, expected in ((["whoami"], self.site["cluster"]["user"]),
                                  (["whoami", "--show-server"], self.site["cluster"]["server"]),
                                  (["config", "current-context"], self.site["cluster"]["context"])):
            if args == command: return expected
        if "scale" in args:
            self.replicas = int(args[-1].split("=")[1])
            return "scaled"
        if "rollout" in args: return "ready"
        if "exec" in args:
            return json.dumps({"status": "verified", "files": 4, "sourceSeconds": 600,
                               "loopPlayback": False, "syncFileCount": 4})
        raise AssertionError(args)

    def run(self, output):
        with mock.patch.object(session, "Forward", side_effect=self.forward), \
             mock.patch.object(session, "source_snapshot", side_effect=lambda _, **kwargs: ("source-uid", self.replicas, self.image)), \
             mock.patch.object(session, "wait_source_stopped"), \
             mock.patch.object(deploy, "oc", side_effect=self.oc), \
             mock.patch.object(session.time, "monotonic", side_effect=self.clock.monotonic), \
             mock.patch.object(session.time, "time", side_effect=self.clock.time), \
             mock.patch.object(session.time, "sleep", side_effect=self.clock.sleep):
            return session.run_session(self.site, self.image, 600, True, False, output, 120, self.media)


class IngestionReliabilityTests(unittest.TestCase):
    def test_empty_or_duplicate_ids_never_count_as_recording_readiness(self):
        client = mock.Mock()
        client.call.return_value = {}
        for ids in ([], ["a", "a"]):
            with self.assertRaisesRegex(deploy.Invalid, "empty or duplicated"):
                session.recording_states(client, ids)
        with self.assertRaisesRegex(deploy.Invalid, "empty or differ"):
            session.observe_progress({}, {}, {}, {}, 0)

    def test_permit_empty_does_not_hide_missing_retained_status(self):
        client = mock.Mock()
        for empty in (None, {}, []):
            client.call.return_value = empty
            with self.assertRaisesRegex(deploy.Invalid, "status IDs differ"):
                session.recording_states(client, ["retained-id"], permit_empty=True)
        client.call.return_value = {}
        self.assertEqual(session.recording_states(client, [], permit_empty=True), {})

    def test_duplicate_sensor_ids_are_rejected_before_registration(self):
        native = NativeFixture()
        for name in session.CAMERAS[:2]:
            native.cameras[name] = {"name": name, "sensorId": "same-id", "sensorIp": SOURCE}
        with self.assertRaisesRegex(deploy.Invalid, "duplicate"):
            session.bootstrap(native.api("source"), native.api("sensor"), native.api("recorder"), SOURCE, VIOS, True)
        self.assertFalse(any(method != "GET" for _, method, _, _ in native.calls))

    def test_partial_accepted_registration_is_discovered_on_retry_not_reposted(self):
        native = NativeFixture()
        sensor = native.api("sensor")
        original = sensor.call
        failed = threading.Event()
        def ambiguous(method, path, data=None):
            result = original(method, path, data)
            if method == "POST" and data["name"] == "Camera" and not failed.is_set():
                failed.set()
                raise urllib.error.URLError("accepted request lost its response")
            return result
        sensor.call = ambiguous
        with self.assertRaises(urllib.error.URLError):
            session.bootstrap(native.api("source"), sensor, native.api("recorder"), SOURCE, VIOS, True)
        retained = {name: item["sensorId"] for name, item in native.cameras.items()}
        native.calls.clear()
        result = session.bootstrap(native.api("source"), sensor, native.api("recorder"), SOURCE, VIOS, True)
        self.assertTrue(all(result["cameras"][name] == ident for name, ident in retained.items()))
        reposted = [data["name"] for service, method, _, data in native.calls if service == "sensor" and method == "POST"]
        self.assertFalse(set(reposted) & set(retained))

    def test_duplicate_proxy_and_retained_stale_recorder_are_not_silently_adopted(self):
        for duplicate in (True, False):
            native = NativeFixture()
            session.bootstrap(native.api("source"), native.api("sensor"), native.api("recorder"), SOURCE, VIOS, True)
            recorder = native.api("recorder")
            original = recorder.call
            def changed(method, path, data=None):
                result = original(method, path, data)
                if path == "/proxy/streams" and duplicate: return result + result[:1]
                if path == "/record/streams" and not duplicate:
                    result[0][next(iter(result[0]))][0]["url"] = "rtsp://old-pod.example.org:30554/live/old-id"
                return result
            recorder.call = changed
            native.calls.clear()
            with self.assertRaisesRegex(deploy.Invalid, "duplicate|retained recorder URL"):
                session.bootstrap(native.api("source"), native.api("sensor"), recorder, SOURCE, VIOS, True)
            self.assertFalse(any(method != "GET" for _, method, _, _ in native.calls))

    def test_advancing_search_fixed_epoch_is_not_fresh_rtsp_progress(self):
        previous, advances, last = {"a": 0}, {"a": 0}, {"a": 0}
        for now, end in ((0, 10), (20, 30), (40, 50)):
            self.assertFalse(session.observe_progress({"a": end}, previous, advances, last, now, minimum_end=1000))
        self.assertEqual(advances, {"a": 0})
        with self.assertRaisesRegex(deploy.Invalid, "stalled"):
            session.observe_progress({"a": 200}, previous, advances, last, 121, minimum_end=1000)

    def test_timeline_requires_explicit_utc_and_accepts_nanoseconds(self):
        client = mock.Mock()
        for value in ("2026-11-16T12:00:00", "2026-11-16T12:00:00-08:00", 123):
            client.call.return_value = [{"endTime": value}]
            with self.assertRaisesRegex(deploy.Invalid, "timezone-qualified UTC"):
                session.timeline_end(client, "native-id")
        client.call.return_value = [{"endTime": "2026-11-16T12:00:00.123456789Z"}]
        self.assertGreater(session.timeline_end(client, "native-id"), 0)

    def test_native_redirect_and_expired_phase_are_not_retried(self):
        with self.assertRaisesRegex(deploy.Invalid, "redirects"):
            session.NoRedirect().redirect_request(None, None, None, None, None, "http://remote.example.org/")
        client = session.API("http://127.0.0.1:12345/api/v1")
        client.deadline = 0
        with mock.patch.object(client.opener, "open") as opener, mock.patch.object(session.time, "sleep") as sleeper:
            with self.assertRaises(session.PhaseDeadline): session.wait_native(lambda: client.call("GET", "/record/status"))
            opener.assert_not_called()
            sleeper.assert_not_called()

    def test_oc_phase_deadline_caps_timeout_and_never_submits_without_budget(self):
        with mock.patch.object(session.time, "monotonic", return_value=100), mock.patch.object(deploy, "oc", return_value="ok") as client:
            self.assertEqual(session.oc_budget(["whoami"], deadline=103), "ok")
            self.assertEqual(client.call_args.kwargs["timeout"], 3)
            client.reset_mock()
            with self.assertRaises(session.PhaseDeadline): session.oc_budget(["whoami"], deadline=100)
            client.assert_not_called()

    def test_active_retained_recorder_blocks_source_start(self):
        runtime = Lifecycle()
        runtime.native.cameras["Camera"] = {"name": "Camera", "sensorId": "retained-id", "sensorIp": SOURCE}
        runtime.active.add("retained-id")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(deploy.Invalid, "do not adopt"):
                runtime.run(Path(directory) / "run.json")
        self.assertFalse(any("scale" in args for args in runtime.oc_calls))
        self.assertCountEqual(runtime.closed, ["sensor", "recorder"])

    def test_cold_unavailable_status_is_never_normalized_to_off(self):
        for missing in (False, True):
            runtime = Lifecycle()
            runtime.native.cameras["Camera"] = {"name": "Camera", "sensorId": "retained-id", "sensorIp": SOURCE}
            original = runtime.clients["recorder"].call
            def unavailable(method, path, data=None):
                if path == "/record/status":
                    return {} if missing else {"retained-id": {"recording_status": "statusUnknown"}}
                return original(method, path, data)
            runtime.clients["recorder"].call = unavailable
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(deploy.Invalid): runtime.run(Path(directory) / "run.json")
            self.assertFalse(any("scale" in args for args in runtime.oc_calls))

    def test_empty_status_does_not_hide_foreign_native_proxy_inventory(self):
        runtime = Lifecycle()
        original = runtime.clients["recorder"].call
        def foreign(method, path, data=None):
            if path == "/proxy/streams": return [{"sensorId": "foreign-id", "proxyUrl": "rtsp://elsewhere.example.org/live/foreign-id"}]
            return original(method, path, data)
        runtime.clients["recorder"].call = foreign
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(deploy.Invalid, "unrelated camera"):
                runtime.run(Path(directory) / "run.json")
        self.assertFalse(any("scale" in args for args in runtime.oc_calls))

    def test_bootstrap_window_exhaustion_does_not_start_a_shortened_acceptance(self):
        runtime = Lifecycle()
        original = session.bootstrap
        def slow(*args, **kwargs):
            result = original(*args, **kwargs)
            runtime.clock.now += 280
            return result
        with mock.patch.object(session, "bootstrap", side_effect=slow), tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(deploy.Invalid, "requested recording window"):
                runtime.run(Path(directory) / "run.json")
        self.assertFalse(runtime.active)
        self.assertEqual(runtime.replicas, 0)

    def test_completed_short_session_proves_progress_and_stops_owned_source(self):
        runtime = Lifecycle()
        with tempfile.TemporaryDirectory() as directory:
            result = runtime.run(Path(directory) / "run.json")
        self.assertEqual(result["status"], "finite-recording-completed")
        self.assertFalse(result["freshInstallAccepted"])
        self.assertEqual(runtime.clock.now, 120)
        self.assertFalse(runtime.active)
        self.assertEqual(runtime.replicas, 0)
        scales = [args for args in runtime.oc_calls if "scale" in args]
        self.assertIn("--current-replicas=0", scales[0])
        self.assertIn("--current-replicas=1", scales[1])
        self.assertNotIn("rules", runtime.closed)  # Saved rules are opt-in, not a startup dependency.
        down_index = next(index for index, args in enumerate(runtime.oc_calls) if "--replicas=0" in args)
        self.assertTrue(all(timeout <= 10 for timeout in runtime.oc_timeouts[down_index - 3:]))

    def test_uncertain_conditional_scale_is_inspected_and_stopped_when_same_owned_source(self):
        runtime = Lifecycle()
        original = runtime.oc
        def ambiguous(args, **kwargs):
            result = original(args, **kwargs)
            if "--replicas=1" in args:
                raise subprocess.TimeoutExpired("oc", kwargs["timeout"])
            return result
        runtime.oc = ambiguous
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(subprocess.TimeoutExpired): runtime.run(Path(directory) / "run.json")
        self.assertEqual(runtime.replicas, 0)
        self.assertFalse(runtime.recording_actions)
        self.assertEqual(sum("scale" in args for args in runtime.oc_calls), 2)

    def test_uncertain_scale_never_adopts_changed_source_identity(self):
        runtime = Lifecycle()
        with mock.patch.object(session, "verify_identity"), \
             mock.patch.object(session, "source_snapshot", return_value=("foreign-source", 1, runtime.image)), \
             mock.patch.object(deploy, "oc") as client:
            with self.assertRaisesRegex(deploy.Invalid, "no cleanup mutation"):
                session.reconcile_source_attempt(runtime.site, ("source-uid", 0, runtime.image), runtime.image, 100)
            client.assert_not_called()

    def test_partial_native_start_still_stops_every_submitted_id(self):
        runtime = Lifecycle()
        original = runtime.clients["recorder"].call
        failed = threading.Event()
        def partial(method, path, data=None):
            result = original(method, path, data)
            if path.endswith("/start") and not failed.is_set():
                failed.set()
                raise urllib.error.URLError("accepted native START lost its response")
            return result
        runtime.clients["recorder"].call = partial
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(urllib.error.URLError): runtime.run(Path(directory) / "run.json")
        submitted = {ident for action, ident in runtime.recording_actions if action == "start"}
        stopped = {ident for action, ident in runtime.recording_actions if action == "stop"}
        self.assertEqual(len(submitted), 4)
        self.assertEqual(stopped, submitted)
        self.assertFalse(runtime.active)
        self.assertEqual(runtime.replicas, 0)

    def test_last_moment_off_recheck_rejects_operator_start_during_baselines(self):
        runtime = Lifecycle()
        original = runtime.clients["recorder"].call
        baselines = []
        def concurrent_operator(method, path, data=None):
            result = original(method, path, data)
            if path.endswith("/timelines"):
                baselines.append(path)
                if len(baselines) == 4:
                    runtime.active.add(next(iter(runtime.native.cameras.values()))["sensorId"])
            return result
        runtime.clients["recorder"].call = concurrent_operator
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(deploy.Invalid): runtime.run(Path(directory) / "run.json")
        self.assertEqual(len(baselines), 4)
        self.assertFalse(runtime.recording_actions)  # Neither START nor STOP adopts the operator's recorder.
        self.assertEqual(runtime.replicas, 1)  # Never remove a source still used by an unowned active recorder.
        self.assertEqual(len(runtime.active), 1)
        self.assertEqual(sum("scale" in args for args in runtime.oc_calls), 1)

    def test_four_start_requests_are_joined_before_partial_failure_returns(self):
        calls, lock = set(), threading.Lock()
        class Client:
            def call(self, method, path):
                with lock: calls.add(path.split("/")[2])
                if path.startswith("/record/b/"): raise urllib.error.URLError("ambiguous start")
        with self.assertRaises(urllib.error.URLError): session.start_recorders(Client(), ["a", "b", "c", "d"])
        self.assertEqual(calls, {"a", "b", "c", "d"})


if __name__ == "__main__":
    unittest.main()
