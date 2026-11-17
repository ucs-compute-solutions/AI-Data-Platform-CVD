#!/usr/bin/env python3
"""Validate and populate only the reviewed Warehouse model and source claims.

Plan-only by default. Execution copies through one UID-bound helper Pod,
preserves every existing file and PVC, and deletes only its own helper Pod.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path, PurePosixPath
import queue
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
import uuid

import warehouse_deploy as deploy


MODEL = "rtdetr_warehouse_v1.0.2.fp16.onnx"
MODEL_SHA = "0a22264542514149bead6e8582499d9758d51e3fde2892d9d2cc378a60426267"
MODEL_CLAIM = "warehouse-perception-models"
MEDIA_CLAIM = "warehouse-source-videos"
HELPER_IMAGE = "busybox@sha256:7a3ebe5bfd1a4a19797d20b0c0bb39d44393e9a03fd852c0865b0f540d868df0"
RUN_LABEL = "cvd.cisco.com/asset-run"
PLAN_LABEL = "cvd.cisco.com/asset-plan"


def media_tools():
    path = Path(__file__).with_name("media") / "prepare-warehouse-hour-videos.py"
    spec = importlib.util.spec_from_file_location("warehouse_media_population", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def local_assets(plan: dict, model: Path, media_dir: Path, *, hour_manifest: Path | None,
                 ffprobe: str, ffmpeg: str) -> tuple[list[dict], dict]:
    source = plan.get("sourceMedia", {})
    seconds, subpath = source.get("seconds"), source.get("subPath")
    deploy.expect(seconds in (600, 3600), "Reviewed plan must declare 600 or 3600 source seconds")
    deploy.expect(isinstance(subpath, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", subpath)
                  and all(part not in ("", ".", "..") for part in subpath.split("/"))
                  and not PurePosixPath(subpath).is_absolute(), "Reviewed source subpath is unsafe")
    deploy.expect(model.is_file() and not model.is_symlink() and sha256(model) == MODEL_SHA,
                  "Provide the exact regular RT-DETR ONNX file matching the recorded SHA256")
    tools = media_tools()
    derived = None
    if seconds == 3600:
        deploy.expect(hour_manifest is not None and hour_manifest.is_file() and not hour_manifest.is_symlink(),
                      "Hour media requires the prepared manifest.json")
        derived = deploy.read_json(hour_manifest)
        deploy.expect(derived.get("kind") == "WarehouseDerivedHourVideos" and derived.get("secondsPerCamera") == 3600
                      and derived.get("repeatedOriginalPasses") == 6 and derived.get("audio") is False,
                      "Hour manifest must identify six passes of the recorded ten-minute sources")
        videos = derived.get("videos")
        deploy.expect(isinstance(videos, list) and len(videos) == 4 and all(isinstance(item, dict) for item in videos)
                      and {item.get("filename") for item in videos} == set(tools.SOURCES),
                      "Hour manifest must contain exactly the four camera files")
    assets = [{"local": model, "claim": MODEL_CLAIM, "relative": MODEL,
               "bytes": model.stat().st_size, "sha256": MODEL_SHA}]
    manifest = {"secondsPerCamera": seconds, "videos": []}
    for filename, original_sha in tools.SOURCES.items():
        path = media_dir / filename
        deploy.expect(path.is_file() and not path.is_symlink(), f"Missing regular source file: {filename}")
        checksum, size = sha256(path), path.stat().st_size
        metadata = tools.validate_metadata(tools.probe(path, ffprobe), output=seconds == 3600,
                                           source_packets=18000 if seconds == 3600 else 0)
        if seconds == 600:
            deploy.expect(checksum == original_sha, f"Original media checksum differs: {filename}")
        else:
            entry = next(item for item in derived["videos"] if item["filename"] == filename)
            deploy.expect(entry.get("sourceSha256") == original_sha and entry.get("sha256") == checksum
                          and entry.get("bytes") == size, f"Hour manifest/hash differs: {filename}")
            # Recheck continuity and all five repeat joins; a stale receipt is
            # not a substitute for probing the actual file being copied.
            metadata = tools.validate_media(path, ffmpeg, ffprobe, {
                "packets": 18000, "width": metadata["width"], "height": metadata["height"]})
        manifest["videos"].append({"filename": filename, "bytes": size, "sha256": checksum, **metadata})
        assets.append({"local": path, "claim": MEDIA_CLAIM, "relative": f"{subpath}/{filename}",
                       "bytes": size, "sha256": checksum})
    if seconds == 3600:
        manifest.update({"repeatedOriginalPasses": 6, "repeatJoinsRevalidated": True,
                         "preparationManifestSha256": sha256(hour_manifest)})
    return assets, manifest


def helper_pod(plan: dict, name: str, run_id: str) -> dict:
    site = plan["site"]
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": site["namespace"],
        "labels": {RUN_LABEL: run_id, PLAN_LABEL: plan["planSha256"][:32]}}, "spec": {
        "restartPolicy": "Never", "activeDeadlineSeconds": 7200, "terminationGracePeriodSeconds": 5,
        "serviceAccountName": "warehouse-gpu", "nodeSelector": site["placement"]["cpuNodeSelector"],
        "imagePullSecrets": [{"name": value} for value in site["auth"]["imagePullSecrets"]],
        "securityContext": {"runAsUser": 1000, "runAsGroup": 2000, "fsGroup": 2000, "runAsNonRoot": True,
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{"name": "populate", "image": deploy.rewrite_image(HELPER_IMAGE, site.get("images", {}).get("repositoryMirrors", {}), site.get("images", {}).get("reviewedImageOverrides", {})),
            "command": ["/bin/sh", "-ec", "trap 'exit 0' TERM INT; while :; do sleep 10; done"],
            "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
            "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"cpu": "1", "memory": "256Mi"}},
            "volumeMounts": [{"name": "models", "mountPath": "/staging/models"}, {"name": "videos", "mountPath": "/staging/videos"}]}],
        "volumes": [{"name": "models", "persistentVolumeClaim": {"claimName": MODEL_CLAIM}},
                    {"name": "videos", "persistentVolumeClaim": {"claimName": MEDIA_CLAIM}}]}}


def consumers(pods: list[dict], excluded_uid: str | None = None) -> list[str]:
    found = []
    for pod in pods:
        if excluded_uid and pod.get("metadata", {}).get("uid") == excluded_uid:
            continue
        if any(row.get("persistentVolumeClaim", {}).get("claimName") in (MODEL_CLAIM, MEDIA_CLAIM)
               for row in pod.get("spec", {}).get("volumes", [])):
            found.append(pod["metadata"]["name"])
    return found


def no_consumers(namespace: str, excluded_uid: str | None = None) -> None:
    pods = json.loads(deploy.oc(["-n", namespace, "get", "pods", "-o", "json"])).get("items", [])
    deploy.expect(not consumers(pods, excluded_uid), "A Pod consumes a target claim; keep workload controllers stopped before population")


def owned_pod(namespace: str, name: str, uid: str, run_id: str) -> dict:
    pod = json.loads(deploy.oc(["-n", namespace, "get", "pod", name, "-o", "json"]))
    deploy.expect(pod.get("metadata", {}).get("uid") == uid and pod["metadata"].get("labels", {}).get(RUN_LABEL) == run_id,
                  "Helper Pod UID or ownership label changed; no action permitted")
    return pod


def execute_in_pod(namespace: str, name: str, uid: str, run_id: str, script: str, arguments: list[str]) -> str:
    owned_pod(namespace, name, uid, run_id)
    return deploy.oc(["-n", namespace, "exec", name, "-c", "populate", "--", "/bin/sh", "-ec", script, "--", *arguments])


PATH_CHECK = r'''
base=$1; relative=$2; wanted_sha=$3; wanted_size=$4
test -d "$base"
test ! -L "$base"
current=$base
saved_ifs=$IFS; IFS=/
for part in $relative; do
 current="$current/$part"
 test ! -L "$current" || { echo 'Refusing a symlink target' >&2; exit 1; }
done
IFS=$saved_ifs
if test -e "$current"; then
 test -f "$current"
 test "$(wc -c < "$current" | tr -d ' ')" = "$wanted_size"
 test "$(sha256sum "$current" | cut -d ' ' -f 1)" = "$wanted_sha"
 echo MATCH
else
 echo ABSENT
fi
'''
PROMOTE = r'''
partial=$1; target=$2; wanted_sha=$3; wanted_size=$4
test -f "$partial"
test ! -L "$partial"
test "$(wc -c < "$partial" | tr -d ' ')" = "$wanted_size"
test "$(sha256sum "$partial" | cut -d ' ' -f 1)" = "$wanted_sha"
# Hard-link creation fails atomically if any target already exists. Keep the
# run-owned partial link; this utility never removes a file from either claim.
ln "$partial" "$target"
test "$(sha256sum "$target" | cut -d ' ' -f 1)" = "$wanted_sha"
'''
PREPARE_PARTIAL = 'test ! -e "$1"; test ! -L "$1"; mkdir -p "$2"'


def base_path(asset: dict) -> str:
    return "/staging/models" if asset["claim"] == MODEL_CLAIM else "/staging/videos"


def copy_assets(plan: dict, name: str, uid: str, run_id: str, assets: list[dict]) -> list[dict]:
    namespace = plan["site"]["namespace"]
    states = []
    for asset in assets:
        result = execute_in_pod(namespace, name, uid, run_id, PATH_CHECK,
            [base_path(asset), asset["relative"], asset["sha256"], str(asset["bytes"])])
        deploy.expect(result in ("MATCH", "ABSENT"), "Unexpected asset inspection result")
        states.append(result)
    subpath = plan["sourceMedia"]["subPath"]
    # An extra source MP4 would change NvStreamer's inventory/quorum.
    listing = execute_in_pod(namespace, name, uid, run_id,
        'if test -d "$1"; then find "$1" -maxdepth 1 -type f -name "*.mp4" -exec basename {} \\; ; fi',
        ["/staging/videos/" + subpath])
    expected_names = {Path(row["relative"]).name for row in assets if row["claim"] == MEDIA_CLAIM}
    deploy.expect(set(listing.splitlines()) <= expected_names, "Source directory contains another MP4; no file was overwritten")
    result = []
    for asset, state in zip(assets, states):
        no_consumers(namespace, uid)
        target = base_path(asset) + "/" + asset["relative"]
        status = "reused-matching"
        partial = None
        if state == "ABSENT":
            parent = str(PurePosixPath(target).parent)
            partial = parent + "/.warehouse-assets-" + run_id + "-" + Path(asset["relative"]).name + ".partial"
            execute_in_pod(namespace, name, uid, run_id, PREPARE_PARTIAL, [partial, parent])
            # Recheck the source immediately before the transfer; remote hashes
            # also detect a source changed while oc cp was reading it.
            deploy.expect(not asset["local"].is_symlink() and asset["local"].stat().st_size == asset["bytes"]
                          and sha256(asset["local"]) == asset["sha256"], "A local asset changed after validation")
            owned_pod(namespace, name, uid, run_id)
            deploy.oc(["-n", namespace, "cp", str(asset["local"]), f"{name}:{partial}", "-c", "populate", "--no-preserve=true"])
            no_consumers(namespace, uid)
            execute_in_pod(namespace, name, uid, run_id, PROMOTE, [partial, target, asset["sha256"], str(asset["bytes"])])
            status = "copied-verified"
        confirmed = execute_in_pod(namespace, name, uid, run_id, PATH_CHECK,
            [base_path(asset), asset["relative"], asset["sha256"], str(asset["bytes"])])
        deploy.expect(confirmed == "MATCH", "Final asset verification failed")
        result.append({"claim": asset["claim"], "path": asset["relative"], "sha256": asset["sha256"],
                       "bytes": asset["bytes"], "status": status, "retainedPartialLink": partial})
    return result


def delete_owned_helper(namespace: str, name: str, uid: str, run_id: str) -> None:
    owned_pod(namespace, name, uid, run_id)
    process = subprocess.Popen(["oc", "proxy", "--address=127.0.0.1", "--port=0", "--accept-hosts=^127\\.0\\.0\\.1$"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = queue.Queue()
    def reader():
        for line in process.stdout:
            lines.put(line)
    threading.Thread(target=reader, daemon=True).start()
    try:
        deadline, port = time.monotonic() + 30, None
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=.25)
            except queue.Empty:
                deploy.expect(process.poll() is None, "Owned API proxy exited before helper cleanup")
                continue
            match = re.search(r"127\.0\.0\.1:([0-9]+)", line)
            if match:
                port = int(match[1])
                break
        deploy.expect(port is not None, "Owned API proxy was not ready for helper cleanup")
        body = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}, "propagationPolicy": "Background"}
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            connection.request("DELETE", f"/api/v1/namespaces/{namespace}/pods/{name}", body=json.dumps(body),
                headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            response.read(65536)
            deploy.expect(200 <= response.status < 300, "UID-preconditioned helper Pod deletion was not acknowledged")
        finally:
            connection.close()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    deadline = time.monotonic() + 60
    while True:
        remaining = deploy.oc(["-n", namespace, "get", "pod", name, "--ignore-not-found", "-o", "json"])
        if not remaining or json.loads(remaining)["metadata"]["uid"] != uid:
            break
        deploy.expect(time.monotonic() < deadline, "Owned helper Pod is still terminating; inspect before another population run")
        time.sleep(1)


def populate(plan: dict, manifests: dict, assets: list[dict], media_manifest: dict, *, execute: bool,
             approved: str | None, receipt: Path, manifest_output: Path) -> dict:
    run_id = uuid.uuid4().hex[:20]
    name = "warehouse-assets-" + run_id
    pod = helper_pod(plan, name, run_id)
    if manifest_output.exists():
        deploy.expect(not manifest_output.is_symlink() and deploy.read_json(manifest_output) == media_manifest,
                      "Existing media manifest differs; choose a new output path")
    summary = {"kind": "warehouse-asset-population", "planSha256": plan["planSha256"],
        "modelSha256": MODEL_SHA, "mediaManifest": media_manifest, "helperPod": pod,
        "files": [{key: value for key, value in row.items() if key != "local"} for row in assets],
        "pvcDeletion": False, "dataDeletion": False}
    if not execute:
        return {"status": "plan-only", **summary}
    deploy.expect(approved == plan["planSha256"], "Use the exact reviewed --approved-plan-sha256")
    deploy.expect(not receipt.exists(), "Use a new asset receipt path; previous receipts are retained")
    site, namespace = plan["site"], plan["site"]["namespace"]
    for args, expected in ((["whoami"], site["cluster"]["user"]), (["whoami", "--show-server"], site["cluster"]["server"]),
                           (["config", "current-context"], site["cluster"]["context"])):
        deploy.expect(deploy.oc(args) == expected, "OpenShift identity, API or context differs from the reviewed plan")
    planned_claims = {row["metadata"]["name"]: row for row in manifests["items"] if row["kind"] == "PersistentVolumeClaim"}
    claim_uids = {}
    for claim in (MODEL_CLAIM, MEDIA_CLAIM):
        deploy.expect(claim in planned_claims, "Asset claim is absent from the reviewed install")
        actual = json.loads(deploy.oc(["-n", namespace, "get", "pvc", claim, "-o", "json"]))
        expected = planned_claims[claim]["spec"]
        deploy.expect(actual.get("status", {}).get("phase") == "Bound" and not actual["metadata"].get("deletionTimestamp")
                      and all(actual["spec"].get(key) == expected.get(key) for key in ("storageClassName", "accessModes")),
                      "Asset PVC binding/access mode/storage class differs from the plan")
        claim_uids[claim] = actual["metadata"]["uid"]
    no_consumers(namespace)
    deploy.oc(["create", "--dry-run=server", "-f", "-", "-o", "name"], data=json.dumps(pod))
    # Creation returns the exact UID; a same-name replacement is never adopted.
    created = json.loads(deploy.oc(["create", "-f", "-", "-o", "json"], data=json.dumps(pod)))
    uid = created["metadata"]["uid"]
    deploy.expect(isinstance(uid, str) and uid, "Created helper did not return an exact UID; inspect its generated name before retrying")
    result = {"status": "failed", "planSha256": plan["planSha256"], "modelSha256": MODEL_SHA,
        "mediaManifest": media_manifest, "claimUids": claim_uids,
        "helper": {"name": name, "uid": uid, "runId": run_id}, "helperPodRemoved": False, "pvcHashesVerified": False,
        "pvcDeletion": False, "dataDeletion": False, "checkedUtc": datetime.now(timezone.utc).isoformat()}
    try:
        deploy.oc(["-n", namespace, "wait", "--for=condition=Ready", "pod/" + name, "--timeout=180s"])
        no_consumers(namespace, uid)
        result["files"] = copy_assets(plan, name, uid, run_id, assets)
        for claim, claim_uid in claim_uids.items():
            deploy.expect(deploy.oc(["-n", namespace, "get", "pvc", claim, "-o", "jsonpath={.metadata.uid}"]) == claim_uid,
                          "An asset claim UID changed during population")
        result["pvcHashesVerified"] = True
        result["status"] = "pass"
    finally:
        try:
            delete_owned_helper(namespace, name, uid, run_id)
            result["helperPodRemoved"] = True
        except Exception as error:
            result["cleanupError"] = str(error)
            result["status"] = "failed-helper-cleanup"
        if result["status"] == "pass" and not manifest_output.exists():
            try:
                with manifest_output.open("x", encoding="utf-8") as stream:
                    json.dump(media_manifest, stream, indent=2)
                    stream.write("\n")
            except OSError:
                result["status"] = "failed-local-manifest-output"
        with receipt.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2)
            stream.write("\n")
    deploy.expect(result["status"] == "pass", "Asset population or owned helper cleanup did not complete; inspect the retained receipt")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-directory", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--media-dir", type=Path, required=True)
    parser.add_argument("--hour-manifest", type=Path)
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--media-manifest-output", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approved-plan-sha256")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        plan, manifests = deploy.load_plan(args.plan_directory)
        ffprobe = shutil.which(args.ffprobe)
        ffmpeg = shutil.which(args.ffmpeg)
        deploy.expect(ffprobe and (plan.get("sourceMedia", {}).get("seconds") != 3600 or ffmpeg),
                      "Install trusted FFprobe; hour join revalidation also needs FFmpeg")
        assets, media_manifest = local_assets(plan, args.model, args.media_dir,
            hour_manifest=args.hour_manifest, ffprobe=ffprobe, ffmpeg=ffmpeg)
        result = populate(plan, manifests, assets, media_manifest, execute=args.execute,
            approved=args.approved_plan_sha256, receipt=args.receipt or args.plan_directory / "assets-receipt.json",
            manifest_output=args.media_manifest_output or args.plan_directory / "media-manifest.json")
        print(json.dumps(result, indent=2))
    except KeyboardInterrupt:
        parser.exit(130, "Interrupted; inspect the asset receipt and verify owned helper cleanup before retrying. Existing data retained.\n")
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError, http.client.HTTPException) as error:
        parser.exit(1, f"FAIL: {error}\nExisting files and PVCs were retained.\n")


if __name__ == "__main__":
    main()
