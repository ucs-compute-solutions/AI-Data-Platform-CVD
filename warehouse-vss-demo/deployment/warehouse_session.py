#!/usr/bin/env python3
"""Finite four-camera session with discovered native camera IDs.

Plan-only by default. Explicit execution starts the existing source, registers
only missing cameras through NVIDIA APIs, records, and stops only its own run.
No API here deletes cameras, rules, clips, volumes or historical incidents.
"""
from __future__ import annotations

import argparse
import concurrent.futures
from datetime import datetime, timedelta
import json
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import warehouse_deploy as deploy

CAMERAS = ("Camera", "Camera_01", "Camera_02", "Camera_03")
SERVICES = {"source": ("warehouse-nvstreamer", 31000), "sensor": ("warehouse-vios-sensor", 30000),
            "recorder": ("warehouse-vios-streamprocessing", 30001), "rules": ("warehouse-alert-bridge", 9080)}
NATIVE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
SOURCE_VERIFICATION = r'''
import hashlib,json,pathlib,sys
expected=json.load(sys.stdin)
config=json.loads(pathlib.Path('/home/vst/vst_release/configs/vst_config.json').read_text())['data']
assert config.get('nv_streamer_loop_playback') is False, 'effective source looping is not disabled'
assert type(config.get('nv_streamer_sync_file_count')) is int and config['nv_streamer_sync_file_count']==4, 'effective synchronization count differs'
for video in expected['videos']:
 path=pathlib.Path('/tmp/nv_streamer/videos')/video['filename']
 assert not path.is_symlink() and path.is_file(), 'expected regular source file missing'
 assert path.stat().st_size==video['bytes'], 'source byte size differs'
 digest=hashlib.sha256()
 with path.open('rb') as stream:
  for block in iter(lambda:stream.read(1024*1024),b''): digest.update(block)
 assert digest.hexdigest()==video['sha256'], 'source checksum differs'
print(json.dumps({'status':'verified','files':4,'sourceSeconds':expected['secondsPerCamera'],'loopPlayback':False,'syncFileCount':4}))
'''


def verify_media_receipt(receipt, seconds):
    deploy.expect(isinstance(receipt, dict) and receipt.get("secondsPerCamera") == seconds, "Provide the verified source-media manifest for the exact source duration")
    videos = receipt.get("videos")
    deploy.expect(isinstance(videos, list) and len(videos) == 4 and {item.get("filename") for item in videos} == {name + ".mp4" for name in CAMERAS}, "Source manifest must describe the four exact media filenames")
    for item in videos:
        deploy.expect(type(item.get("bytes")) is int and item["bytes"] > 0 and re.fullmatch(r"[a-f0-9]{64}", item.get("sha256", ""))
                      and abs(float(item.get("seconds", 0)) - seconds) <= .1, "Source manifest lacks verified bytes, SHA256 or exact duration")
    return {"secondsPerCamera": seconds, "videos": videos}


def wait_native(action, *, seconds=90, sleep=time.sleep):
    deadline = time.monotonic() + seconds
    while True:
        try: return action()
        except (deploy.Invalid, urllib.error.URLError, json.JSONDecodeError, TimeoutError) as error:
            if isinstance(error, PhaseDeadline): raise
            deploy.expect(time.monotonic() < deadline, "Native API/inventory readiness did not converge within its bounded retry window")
            sleep(2)


def observe_progress(current, previous, advances, last_progress, now, stall_seconds=120, minimum_end=None):
    deploy.expect(bool(advances) and set(current) == set(advances) == set(last_progress),
                  "Recording progress IDs are empty or differ from the discovered camera map")
    for ident, end in current.items():
        if end is not None and previous.get(ident) is not None:
            deploy.expect(end >= previous[ident], "A recording timeline moved backwards; readiness revoked")
            if end > previous[ident] and (minimum_end is None or end >= minimum_end):
                advances[ident] += 1
                last_progress[ident] = now
        if end is not None: previous[ident] = end
        deploy.expect(now - last_progress[ident] <= stall_seconds, "A recording timeline stalled; readiness revoked and owned cleanup required")
    return all(value >= 2 for value in advances.values())


class PhaseDeadline(deploy.Invalid):
    """The current API phase ended before another operation could begin."""


def oc_budget(arguments, *, data=None, deadline=None, timeout=10):
    """Never let an oc request consume another lifecycle phase's reserve."""
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise PhaseDeadline("OpenShift phase deadline expired; no request sent")
        timeout = min(timeout, remaining)
    return deploy.oc(arguments, data=data, timeout=timeout)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise deploy.Invalid("Native API redirects are not permitted outside the owned local forward")


class API:
    def __init__(self, base):
        parsed = urllib.parse.urlsplit(base)
        deploy.expect(parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port and not parsed.username and not parsed.password,
                      "Native API client uses an explicit verified local port-forward only")
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.deadline, self.timeout = None, 25

    def call(self, method, path, data=None):
        timeout = self.timeout
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0: raise PhaseDeadline("Native API phase deadline expired")
            timeout = min(timeout, remaining)
        body = (json.dumps(data).encode() if data is not None else b"") if method == "POST" else None
        request = urllib.request.Request(self.base + path, data=body, method=method,
                                        headers={"Accept": "application/json", "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read(1_048_577)
                deploy.expect(len(raw) <= 1_048_576, "Native API response exceeds the bounded inventory limit")
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as error:
            raise deploy.Invalid(f"Native {method} {path.split('?')[0]} returned HTTP {error.code}; response body suppressed") from error


def source_urls(api, stable_host):
    raw = api.call("GET", "/sensor/streams")
    deploy.expect(isinstance(raw, list), "NvStreamer stream inventory is not an array")
    result = {}
    for wrapper in raw:
        deploy.expect(isinstance(wrapper, dict), "Malformed NvStreamer stream wrapper")
        for streams in wrapper.values():
            deploy.expect(isinstance(streams, list), "Malformed NvStreamer stream list")
            for stream in streams:
                deploy.expect(isinstance(stream, dict), "Malformed NvStreamer source stream")
                name = stream.get("name")
                if name not in CAMERAS: continue
                deploy.expect(name not in result, "Duplicate source camera name")
                parsed = urllib.parse.urlsplit(stream.get("url", ""))
                deploy.expect(parsed.scheme == "rtsp" and parsed.hostname and parsed.port in range(31554, 31558)
                              and parsed.path == f"/nvstream/tmp/nv_streamer/videos/{name}.mp4"
                              and parsed.query in ("", "transport=tcp")
                              and not parsed.username and not parsed.password and not parsed.fragment, "Unexpected or credential-bearing NvStreamer URL")
                query = dict(urllib.parse.parse_qsl(parsed.query))
                query["transport"] = "tcp"
                result[name] = urllib.parse.urlunsplit(("rtsp", f"{stable_host}:{parsed.port}", parsed.path, urllib.parse.urlencode(query), ""))
    deploy.expect(set(result) == set(CAMERAS), "The four named source files must be present before camera registration")
    return result


def sensors(api):
    raw = api.call("GET", "/sensor/list")
    deploy.expect(isinstance(raw, list), "VIOS sensor/list is not an array")
    result = {}
    identifiers = set()
    for item in raw:
        deploy.expect(isinstance(item, dict), "Malformed VIOS sensor record")
        name = item.get("name")
        ident = item.get("sensorId")
        deploy.expect(name in CAMERAS and name not in result and isinstance(ident, str)
                      and NATIVE_ID.fullmatch(ident) and ident not in identifiers,
                      "Fresh target contains an extra, duplicate or malformed camera; no change")
        result[name] = item
        identifiers.add(ident)
    return result


def recording_states(api, ids, *, permit_empty=False):
    ids = list(ids)
    deploy.expect(len(ids) == len(set(ids)) and (ids or permit_empty), "Recorder ID inventory is empty or duplicated")
    raw = api.call("GET", "/record/status")
    if permit_empty and not ids and raw in (None, {}, []): return {}
    deploy.expect(isinstance(raw, dict) and set(raw) == set(ids), "Recorder status IDs differ from the discovered camera map")
    deploy.expect(all(isinstance(value, dict) and isinstance(value.get("recording_status"), str) for value in raw.values()),
                  "Recorder status entry is malformed")
    return {ident: value.get("recording_status") for ident, value in raw.items()}


def proxy_streams(api):
    raw = api.call("GET", "/proxy/streams")
    deploy.expect(isinstance(raw, list) and all(isinstance(item, dict) and isinstance(item.get("sensorId"), str) for item in raw),
                  "VIOS proxy inventory is not an array")
    result = {item["sensorId"]: item for item in raw}
    deploy.expect(len(result) == len(raw), "VIOS proxy inventory contains duplicate IDs")
    return result


def recorder_streams(api):
    raw = api.call("GET", "/record/streams")
    if raw is None: raw = []
    deploy.expect(isinstance(raw, list) and all(isinstance(item, dict) and len(item) == 1 for item in raw),
                  "Recorder inventory is not an array")
    result = {}
    for wrapper in raw:
        ident, streams = next(iter(wrapper.items()))
        deploy.expect(ident not in result, "Recorder inventory contains duplicate IDs")
        deploy.expect(isinstance(streams, list) and len(streams) == 1 and isinstance(streams[0], dict)
                      and isinstance(streams[0].get("url"), str), "Recorder stream entry is malformed")
        result[ident] = streams[0]
    return result


def inactive_cameras(sensor, recorder, stable_source):
    """Prove retained OFF states before a source start; missing is not OFF."""
    initial = sensors(sensor)
    for item in initial.values():
        deploy.expect(item.get("sensorIp") == stable_source, "A retained camera advertises another source; review a separate preserve-ID migration")
    ids = [item["sensorId"] for item in initial.values()]
    states = recording_states(recorder, ids, permit_empty=not ids)
    deploy.expect(all(value == "off" for value in states.values()), "A retained recorder is active or unavailable; do not adopt another run")
    deploy.expect(set(proxy_streams(recorder)) <= set(ids) and set(recorder_streams(recorder)) <= set(ids),
                  "Retained native inventories contain an unrelated camera; no source start or adoption")
    return initial


def bootstrap(source, sensor, recorder, stable_source, stable_vios, execute=False, sleep=time.sleep):
    desired = source_urls(source, stable_source)
    initial = inactive_cameras(sensor, recorder, stable_source)
    missing = [name for name in CAMERAS if name not in initial]
    plan = {"missingCameraNames": missing, "retainedCameraNames": sorted(initial),
            "nativeActions": ["POST /sensor/add for missing names only", "wait for native proxy events", "POST /record/stream/add only if missing"],
            "hardCodedCameraIds": False, "cameraDeletion": False}
    if not execute: return plan
    # Register as one bounded burst so a synchronized source can reach quorum.
    def add(name):
        response = sensor.call("POST", "/sensor/add", {"name": name, "sensorUrl": desired[name], "username": "", "password": ""})
        deploy.expect(isinstance(response, dict) and isinstance(response.get("sensorId"), str)
                      and NATIVE_ID.fullmatch(response["sensorId"]), "Native sensor/add did not return a fresh sensorId")
        return name, response["sensorId"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        created = dict(pool.map(add, missing))
    deadline = time.monotonic() + 90
    while True:
        current = sensors(sensor)
        deploy.expect(set(initial) <= set(current)
                      and all(current[name]["sensorId"] == item["sensorId"] for name, item in initial.items())
                      and all(name not in current or current[name]["sensorId"] == ident for name, ident in created.items()),
                      "Native camera IDs changed during registration")
        if set(current) == set(CAMERAS): break
        deploy.expect(time.monotonic() < deadline, "Four native sensor registrations did not converge; retained partial registrations were not deleted")
        sleep(2)
    mapping = {name: current[name]["sensorId"] for name in CAMERAS}
    deploy.expect(len(set(mapping.values())) == 4 and all(mapping[name] == ident for name, ident in created.items()), "Native camera IDs changed during registration")
    for name, item in initial.items():
        deploy.expect(mapping[name] == item["sensorId"], "A retained native camera identity changed")
    deadline = time.monotonic() + 90
    while True:
        proxies = proxy_streams(recorder)
        if set(proxies) == set(mapping.values()): break
        deploy.expect(not (set(proxies) - set(mapping.values())), "Unexpected proxy camera; no registration changes")
        deploy.expect(time.monotonic() < deadline, "Native Sensor-to-StreamProcessing proxy registration did not converge; inspect SDR/network before retry")
        sleep(2)
    records = recorder_streams(recorder)
    registered = set(records)
    deploy.expect(registered <= set(mapping.values()), "Recorder inventory contains an unrelated camera")
    for name, ident in mapping.items():
        parsed = urllib.parse.urlsplit(proxies[ident].get("proxyUrl", ""))
        deploy.expect(parsed.scheme == "rtsp" and parsed.hostname == stable_vios and parsed.port == 30554
                      and parsed.path == "/live/" + ident and not parsed.username and not parsed.password
                      and not parsed.query and not parsed.fragment,
                      "VIOS must advertise its stable Service/shared live listener")
        if ident in records:
            deploy.expect(records[ident]["url"] == proxies[ident]["proxyUrl"],
                          "A retained recorder URL differs from its stable native proxy; no migration performed")
        if ident not in registered:
            recorder.call("POST", "/record/stream/add", {"id": ident, "url": proxies[ident]["proxyUrl"]})
    def four_off():
        deploy.expect(all(value == "off" for value in recording_states(recorder, mapping.values()).values()), "Four discovered recorders are not inactive")
        return True
    wait_native(four_off, sleep=sleep)
    return {"format": 1, "cameras": mapping, "sourceUrls": desired, "sourceHost": stable_source,
            "viosHost": stable_vios, "createdCameraNames": sorted(created), "recorders": "four-off"}


class Forward:
    def __init__(self, namespace, service, port, *, deadline=None):
        self.process = subprocess.Popen(["oc", "-n", namespace, "port-forward", "service/" + service, ":" + str(port), "--address", "127.0.0.1"],
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        lines = queue.Queue()
        def read():
            for line in self.process.stdout: lines.put(line)
        threading.Thread(target=read, daemon=True).start()
        deadline = min(time.monotonic() + 30, deadline if deadline is not None else float("inf"))
        while time.monotonic() < deadline:
            try: line = lines.get(timeout=.2)
            except queue.Empty:
                if self.process.poll() is not None: break
                continue
            match = re.search(r"Forwarding from 127\.0\.0\.1:([0-9]+) ->", line)
            if match:
                self.api = API(f"http://127.0.0.1:{match.group(1)}/api/v1")
                return
        self.close()
        raise deploy.Invalid("Could not establish owned local API forward to " + service)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try: self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


def source_snapshot(namespace, *, deadline=None):
    value = json.loads(oc_budget(["-n", namespace, "get", "deployment", "warehouse-nvstreamer", "-o", "json"], deadline=deadline))
    spec = value["spec"]["template"]["spec"]
    containers = [item for item in spec["containers"] if item["name"] == "nvstreamer"]
    deploy.expect(len(containers) == 1, "Expected one NvStreamer source container")
    return value["metadata"]["uid"], value["spec"].get("replicas", 1), containers[0]["image"]


def wait_source_stopped(namespace, uid, image, *, seconds=120, sleep=time.sleep, deadline=None):
    """Observe source Pods gone, including terminating Pods; never delete one."""
    deadline, original_selector = min(time.monotonic() + seconds, deadline if deadline is not None else float("inf")), None
    while True:
        value = json.loads(oc_budget(["-n", namespace, "get", "deployment", "warehouse-nvstreamer", "-o", "json"], deadline=deadline))
        spec = value["spec"]["template"]["spec"]
        containers = [item for item in spec["containers"] if item["name"] == "nvstreamer"]
        deploy.expect(value["metadata"]["uid"] == uid and value["spec"].get("replicas", 1) == 0
                      and len(containers) == 1 and containers[0]["image"] == image,
                      "Stopped-source identity, reviewed image or desired replicas changed during cleanup")
        selector = value["spec"].get("selector", {})
        # The source candidate uses an exact matchLabels selector. Do not guess
        # at a different controller's selector or treat an unsupported one as empty.
        labels = selector.get("matchLabels", {})
        deploy.expect(labels and not selector.get("matchExpressions") and set(selector) <= {"matchLabels", "matchExpressions"},
                      "Source selector is not the reviewed nonempty matchLabels contract")
        if original_selector is None: original_selector = selector
        deploy.expect(selector == original_selector, "Source selector changed during cleanup")
        selected = ",".join(f"{key}={labels[key]}" for key in sorted(labels))
        pods = json.loads(oc_budget(["-n", namespace, "get", "pods", "--selector", selected, "-o", "json"], deadline=deadline))
        deploy.expect(isinstance(pods, dict) and isinstance(pods.get("items"), list), "Source Pod inventory is malformed")
        if not pods["items"]: return
        deploy.expect(time.monotonic() < deadline, "Source Pods remain after scale-to-zero; cleanup is incomplete, do not restart")
        sleep(2)


def timeline_end(api, ident):
    raw = api.call("GET", f"/storage/{ident}/timelines")
    deploy.expect(isinstance(raw, list), "Native timeline inventory is malformed")
    if not raw: return None
    def timestamp(value):
        deploy.expect(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+]00:00)", value),
                      "Native recording timeline must use real timezone-qualified UTC")
        value = re.sub(r"\.(\d{1,9})(?=Z|[+]00:00$)", lambda match: "." + match.group(1)[:6].ljust(6, "0"), value)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        deploy.expect(parsed.utcoffset() == timedelta(0), "Native recording timeline is not UTC")
        return parsed.timestamp()
    deploy.expect(all(isinstance(item, dict) and "endTime" in item for item in raw), "Native timeline entry is malformed")
    return max(timestamp(item["endTime"]) for item in raw)


def verify_identity(site, *, deadline=None):
    for args, expected in ((["whoami"], site["cluster"]["user"]), (["whoami", "--show-server"], site["cluster"]["server"]), (["config", "current-context"], site["cluster"]["context"])):
        deploy.expect(oc_budget(args, deadline=deadline) == expected, "OpenShift identity, API or context changed")


def reconcile_source_attempt(site, initial, image, deadline):
    """Inspect an uncertain conditional scale result; never adopt changed state."""
    verify_identity(site, deadline=deadline)
    current = source_snapshot(site["namespace"], deadline=deadline)
    deploy.expect(current[0] == initial[0] and current[2] == image and current[1] in (0, 1),
                  "Uncertain source scale changed identity, image or replica contract; no cleanup mutation")
    if current[1] == 0:
        wait_source_stopped(site["namespace"], initial[0], image, deadline=deadline)
        return False
    # Only an attempted --current-replicas=0 start of this exact stopped
    # deployment reaches here. The caller must still prove native OFF before
    # undoing it; concurrent lifecycle controllers are not supported.
    return True


def start_recorders(recorder, ids):
    deploy.expect(len(ids) == 4 and len(set(ids)) == 4, "Concurrent START requires exactly four discovered IDs")
    barrier = threading.Barrier(4, timeout=5)
    def start(ident):
        barrier.wait()
        return recorder.call("POST", f"/record/{ident}/start")
    # Join every submitted request before the caller's finally sends STOP.
    # No retry of an ambiguous native START can race with owned cleanup.
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(start, ids))


def run_session(site, image, source_seconds, execute, replay_rules, output, record_seconds=None, media_receipt=None):
    deploy.validate_site(site)
    deploy.rewrite_image(image, {})
    deploy.expect(source_seconds in (600, 3600), "Select verified 600-second originals or prepared 3600-second media")
    deploy.expect(record_seconds is None or record_seconds in (90, 120), "Short acceptance duration is 90 or 120 seconds")
    plan = {"namespace": site["namespace"], "sourceImage": image, "sourceSeconds": source_seconds,
            "sourceStopDeadlineSecondsAfterScale": source_seconds - 60,
            "recordingStopDeadlineSecondsAfterScale": source_seconds - 200,
            "actions": ["verify identity and stopped source", "start owned source", "discover/register native camera IDs", "record four cameras", "verify timeline progression", "stop four recorders before owned source"],
            "replaySavedRules": replay_rules, "dataDeletion": False, "nodeChanges": False,
            "gpuReservationChange": "NvStreamer source 0 to 1 only", "recordSeconds": record_seconds}
    if not execute: return {"status": "plan-only", **plan}
    media_receipt = verify_media_receipt(media_receipt, source_seconds)
    deploy.expect(not output.exists(), "Use a new run receipt path; do not overwrite prior camera maps")
    verify_identity(site)
    namespace = site["namespace"]
    initial = source_snapshot(namespace)
    deploy.expect(initial[1] == 0 and initial[2] == image, "Source must be stopped with the exact reviewed image")
    wait_source_stopped(namespace, initial[0], image)
    source_owned, source_start_attempted, attempted, forwards, mapping = False, False, False, {}, None
    source_deadline = None
    try:
        stable_source = f"warehouse-nvstreamer.{namespace}.svc.{site['cluster']['domain']}"
        stable_vios = f"warehouse-vios-streamprocessing.{namespace}.svc.{site['cluster']['domain']}"
        # Prove inactivity before starting any source. An abort caused by an
        # active/foreign recorder must never stop that recorder's source.
        for name in ("sensor", "recorder"):
            service, port = SERVICES[name]
            forwards[name] = Forward(namespace, service, port)
        sensor, recorder = (forwards[name].api for name in ("sensor", "recorder"))
        wait_native(lambda: sensors(sensor))
        inactive_cameras(sensor, recorder, stable_source)
        verify_identity(site)
        deploy.expect(source_snapshot(namespace) == initial, "Source identity, image or replicas changed before start")
        started = time.monotonic()
        source_deadline = started + source_seconds - 60
        recording_deadline = source_deadline - 140  # 20s recorder STOP + 120s source absence proof.
        registration_deadline = recording_deadline - (record_seconds or 120) - 25
        source_start_attempted = True
        oc_budget(["-n", namespace, "scale", "deployment/warehouse-nvstreamer", "--current-replicas=0", "--replicas=1"], deadline=registration_deadline)
        source_owned = True
        oc_budget(["-n", namespace, "rollout", "status", "deployment/warehouse-nvstreamer", "--timeout=180s"], deadline=registration_deadline, timeout=185)
        deploy.expect(source_snapshot(namespace, deadline=registration_deadline) == (initial[0], 1, image), "Owned source identity, image or replicas changed during startup")
        verified = json.loads(oc_budget(["-n", namespace, "exec", "-i", "deployment/warehouse-nvstreamer", "-c", "nvstreamer", "--", "python3", "-c", SOURCE_VERIFICATION], data=json.dumps(media_receipt), deadline=registration_deadline, timeout=90))
        deploy.expect(verified == {"status": "verified", "files": 4, "sourceSeconds": source_seconds, "loopPlayback": False, "syncFileCount": 4}, "Actual source/config verification did not pass")
        service, port = SERVICES["source"]
        forwards["source"] = Forward(namespace, service, port, deadline=registration_deadline)
        source = forwards["source"].api
        if replay_rules:
            service, port = SERVICES["rules"]
            forwards["rules"] = Forward(namespace, service, port, deadline=registration_deadline)
        source.deadline, source.timeout = registration_deadline, 5
        wait_native(lambda: source_urls(source, stable_source))
        deploy.expect(time.monotonic() + 120 < recording_deadline, "Source readiness used too much of the finite source; cleanup before retry")
        for api in (source, sensor, recorder):
            api.deadline, api.timeout = registration_deadline, 5
        mapping = bootstrap(source, sensor, recorder, stable_source, stable_vios, True)
        deploy.write_json(output, mapping)
        ids = list(mapping["cameras"].values())
        previous = {ident: timeline_end(recorder, ident) for ident in ids}
        # Recheck after registration and the baseline GETs, not only before
        # their bounded retries. Never report a shortened acceptance as done.
        deploy.expect(time.monotonic() + (record_seconds or 120) + 25 < recording_deadline,
                      "Native registration used too much of the finite source for the requested recording window")
        deploy.expect(all(value == "off" for value in recording_states(recorder, ids).values()),
                      "A recorder changed after baseline collection; no START or adoption")
        recording_epoch = time.time()
        recorder.deadline, recorder.timeout = min(recording_deadline, time.monotonic() + 25), 20
        attempted = True
        start_recorders(recorder, ids)
        start_deadline = time.monotonic() + 20
        while not all(value == "user" for value in recording_states(recorder, ids).values()):
            deploy.expect(time.monotonic() < start_deadline, "Four native recorders did not reach USER after their accepted starts")
            time.sleep(1)
        recording_started = time.monotonic()
        if replay_rules:
            rules_api = forwards["rules"].api
            rules_api.deadline, rules_api.timeout = min(recording_deadline, recording_started + 20), 5
            inventory = rules_api.call("GET", "/realtime")
            deploy.expect(isinstance(inventory, dict) and isinstance(inventory.get("rules"), list), "Saved-rule inventory is malformed")
            rules = inventory["rules"]
            if rules:
                result = rules_api.call("POST", "/realtime/replay", {})
                deploy.expect(isinstance(result, dict) and result.get("failed") == 0 and result.get("replayed") == len(rules), "Saved-rule runtime reconnection did not complete")
        deadline = min(recording_deadline,
                       recording_started + record_seconds if record_seconds else float("inf"))
        recorder.deadline, recorder.timeout = deadline, 3
        advances, ready = {ident: 0 for ident in ids}, False
        last_progress = {ident: recording_started for ident in ids}
        while time.monotonic() < deadline:
            try:
                states = recording_states(recorder, ids)
                deploy.expect(all(value == "user" for value in states.values()), "A recorder left USER state; readiness revoked")
                progressed = observe_progress({ident: timeline_end(recorder, ident) for ident in ids}, previous, advances, last_progress, time.monotonic(), minimum_end=recording_epoch)
            except PhaseDeadline:
                break
            if not ready and progressed:
                print("RECORDING_READY: four native timelines advance. Verify fresh UI alerts, matching clips and reports separately.", flush=True)
                ready = True
            deploy.expect(ready or time.monotonic() - recording_started < 120, "Four recording timelines did not advance within the startup bound")
            time.sleep(min(20, max(0, deadline - time.monotonic())))
        deploy.expect(ready, "Recording readiness was not established")
        return {"status": "finite-recording-completed", "freshInstallAccepted": False, "cameraMap": mapping["cameras"]}
    finally:
        inactive = not attempted
        if attempted:
            recorder = forwards["recorder"].api
            ids = list(mapping["cameras"].values())
            cleanup_deadline = min(source_deadline, time.monotonic() + 20)
            recorder.deadline, recorder.timeout = cleanup_deadline, 3
            errors = []
            for ident in ids:
                try: recorder.call("POST", f"/record/{ident}/stop")
                except Exception: errors.append(ident)
            while time.monotonic() < cleanup_deadline:
                try: inactive = all(value == "off" for value in recording_states(recorder, ids).values())
                except Exception: inactive = False
                if inactive: break
                time.sleep(min(1, max(0, cleanup_deadline - time.monotonic())))
            print("Four recorder OFF states verified." if inactive else "CLEANUP FAILED: recorder inactivity is not proved; inspect before any restart.", flush=True)
        try:
            if source_start_attempted and not source_owned:
                source_owned = reconcile_source_attempt(site, initial, image, source_deadline)
            if source_owned and inactive:
                if not attempted:
                    # Registration may have failed after an external/runtime
                    # change. Do not infer OFF merely because START was never
                    # sent by this process.
                    cleanup_deadline = min(source_deadline, time.monotonic() + 20)
                    for api in (sensor, recorder):
                        api.deadline, api.timeout = cleanup_deadline, 3
                    inactive_cameras(sensor, recorder, stable_source)
                verify_identity(site, deadline=source_deadline)
                current = source_snapshot(namespace, deadline=source_deadline)
                deploy.expect(current[0] == initial[0] and current[2] == image and current[1] == 1, "Owned source identity changed; no scale request")
                oc_budget(["-n", namespace, "scale", "deployment/warehouse-nvstreamer", "--current-replicas=1", "--replicas=0"], deadline=source_deadline)
                wait_source_stopped(namespace, initial[0], image, deadline=source_deadline)
                deploy.expect(time.monotonic() < source_deadline, "Owned source absence was proved after its conservative finite-source deadline")
                print("Owned source stopped: desired replicas zero and matching Pods absent; recordings, rules and camera IDs retained.", flush=True)
        finally:
            for forward in forwards.values(): forward.close()
        deploy.expect(inactive, "Recording cleanup is incomplete")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--plan-directory", type=Path, help="Use the reviewed site, image pin and media settings without retyping them")
    inputs.add_argument("--site", type=Path, help="Alternate explicit-input mode; also requires --expected-source-image")
    parser.add_argument("--expected-source-image")
    parser.add_argument("--source-seconds", type=int, choices=(600, 3600))
    parser.add_argument("--record-seconds", type=int, choices=(90, 120), help="Short acceptance cycle; omitted runs to the conservative source deadline")
    parser.add_argument("--media-manifest", type=Path, help="Required for execution: exact ffprobe-validated media manifest, rechecked inside the source container")
    parser.add_argument("--camera-map-output", type=Path, default=Path("warehouse-run-cameras.json"))
    parser.add_argument("--replay-saved-rules", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        if args.plan_directory:
            deploy.expect(args.expected_source_image is None and args.source_seconds is None, "The reviewed plan already binds the source image and duration")
            plan, manifests = deploy.load_plan(args.plan_directory)
            site = plan["site"]
            source = next(item for item in manifests["items"] if deploy.reference_key(item) == "Deployment/warehouse-nvstreamer")
            image = next(item["image"] for item in deploy.pod_spec(source)["containers"] if item["name"] == "nvstreamer")
            seconds = plan["sourceMedia"]["seconds"]
        else:
            deploy.expect(bool(args.expected_source_image), "Explicit --site mode requires --expected-source-image")
            site, image, seconds = deploy.read_json(args.site), args.expected_source_image, args.source_seconds or 600
        print(json.dumps(run_session(site, image, seconds,
                                     args.execute, args.replay_saved_rules, args.camera_map_output, args.record_seconds,
                                     deploy.read_json(args.media_manifest) if args.media_manifest else None), indent=2))
    except KeyboardInterrupt:
        print("Session interrupted; owned cleanup ran. Require verified OFF and stopped-source output.", file=sys.stderr)
        raise SystemExit(130)
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError, subprocess.SubprocessError) as error:
        parser.exit(1, "FAIL: " + str(error) + "\n")


if __name__ == "__main__":
    main()
