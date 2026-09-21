"""Offline safety tests for the Warehouse/CVD demo-mode controls.

The tests copy the production scripts into a temporary directory and place
stateful fake ``oc`` and ``helm`` executables first in ``PATH``.  No command in
this module can discover or contact a real OpenShift cluster.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPO_ROOT / "demos" / "warehouse-operations"
SET_MODE = SOURCE_ROOT / "scripts" / "set-demo-mode.sh"
SHOW_STATUS = SOURCE_ROOT / "scripts" / "show-status.sh"
PROFILES = SOURCE_ROOT / "profiles.example.yaml"
ACK = "GPU_MODE_SWITCH_APPROVED_PRESERVE_ALL_DATA"


FAKE_OC = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path


state_path = Path(os.environ["FAKE_CLUSTER_STATE"])
log_path = Path(os.environ["FAKE_COMMAND_LOG"])
args = sys.argv[1:]


def load_state():
    return json.loads(state_path.read_text(encoding="utf-8"))


def save_state(state):
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def log_command():
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write("oc " + " ".join(args) + "\n")


def emit(value):
    if isinstance(value, (dict, list)):
        print(json.dumps(value, separators=(",", ":")))
    else:
        print(value)


def resource_key(namespace, ref):
    return f"{namespace}|{ref.lower()}"


def resource_object(record):
    result = {
        "apiVersion": "apps/v1",
        "kind": record["kind"],
        "metadata": {"name": record["name"], "namespace": record["namespace"]},
        "spec": {"replicas": record["spec"]},
        "status": {
            "readyReplicas": record["ready"],
            "availableReplicas": record["available"],
        },
    }
    if record.get("runtime"):
        result["spec"]["selector"] = {"matchLabels": {"test-controller": record["slug"]}}
        result["spec"]["template"] = {
            "metadata": {"labels": {"test-controller": record["slug"]}},
            "spec": {
                "containers": [
                    {
                        "name": "test",
                        "resources": {"requests": {"nvidia.com/gpu": str(record["gpu"])}},
                    }
                ]
            },
        }
    if record.get("retention"):
        when_deleted, when_scaled = record["retention"].split("/", 1)
        result["spec"]["persistentVolumeClaimRetentionPolicy"] = {
            "whenDeleted": when_deleted,
            "whenScaled": when_scaled,
        }
    return result


def fail_if_requested(state):
    needle = os.environ.get("FAKE_FAIL_SUBSTRING", "")
    rendered = "oc " + " ".join(args)
    if needle and needle in rendered and not state.get("failure_injected", False):
        state["failure_injected"] = True
        save_state(state)
        print("injected oc failure", file=sys.stderr)
        raise SystemExit(19)


log_command()
state = load_state()
fail_if_requested(state)

if args == ["whoami"]:
    emit("system:admin")
elif args == ["whoami", "--show-server"]:
    emit("https://api.example.test:6443")
elif args == ["config", "current-context"]:
    emit("admin")
elif args == ["version", "-o", "json"]:
    emit({"serverVersion": {"gitVersion": "v1.33.9"}})
elif args[:4] == ["get", "clusterversion", "version", "-o"]:
    emit({"status": {"desired": {"version": "4.20.18"}}})
elif args[:3] == ["get", "node", "gpu-worker"]:
    emit(
        {
            "metadata": {"name": "gpu-worker"},
            "spec": {"unschedulable": bool(state["node_cordoned"])},
            "status": {
                "allocatable": {"nvidia.com/gpu": "8"},
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        }
    )
elif args[:2] == ["adm", "uncordon"]:
    state["node_cordoned"] = False
    save_state(state)
    emit("node/gpu-worker uncordoned")
elif args[:2] == ["adm", "cordon"]:
    state["node_cordoned"] = True
    save_state(state)
    emit("node/gpu-worker cordoned")
elif args[:5] == ["-n", "nvidia-gpu-operator", "get", "clusterpolicy", "gpu-cluster-policy"]:
    emit({"status": {"state": "ready"}})
elif len(args) >= 4 and args[0] == "-n" and args[2:4] == ["get", "pvc"]:
    emit(
        {
            "items": [
                {
                    "metadata": {"name": "retained-test-pvc", "namespace": args[1]},
                    "status": {"phase": "Bound"},
                }
            ]
        }
    )
elif args[:3] == ["get", "pods", "-A"]:
    pods = []
    seen = set()
    for record in state["resources"].values():
        if not record.get("runtime") or record["spec"] <= 0:
            continue
        identity = (record["namespace"], record["kind"], record["name"])
        if identity in seen:
            continue
        seen.add(identity)
        for index in range(record["spec"]):
            pods.append(
                {
                    "metadata": {
                        "name": f"{record['slug']}-{index}",
                        "namespace": record["namespace"],
                    },
                    "spec": {
                        "nodeName": "gpu-worker",
                        "containers": [
                            {
                                "name": "test",
                                "resources": {
                                    "requests": {"nvidia.com/gpu": str(record["gpu"])}
                                },
                            }
                        ],
                    },
                    "status": {"phase": "Running"},
                }
            )
    for index in range(int(state.get("unrelated_gpu_pods", 0))):
        pods.append(
            {
                "metadata": {"name": f"unrelated-{index}", "namespace": "other"},
                "spec": {
                    "nodeName": "gpu-worker",
                    "containers": [
                        {
                            "name": "other",
                            "resources": {"requests": {"nvidia.com/gpu": "1"}},
                        }
                    ],
                },
                "status": {"phase": "Running"},
            }
        )
    emit({"items": pods})
elif len(args) >= 6 and args[0] == "-n" and args[2:4] == ["get", "pods"] and "-l" in args:
    namespace = args[1]
    selector = args[args.index("-l") + 1]
    slug = selector.split("=", 1)[1]
    records = [
        row
        for row in state["resources"].values()
        if row.get("runtime") and row["namespace"] == namespace and row["slug"] == slug
    ]
    pods = []
    if records:
        record = records[0]
        for index in range(record["spec"]):
            pods.append(
                {
                    "metadata": {"name": f"{slug}-{index}", "namespace": namespace},
                    "spec": {"nodeName": "gpu-worker"},
                    "status": {
                        "phase": "Running",
                        "conditions": [{"type": "PodScheduled", "status": "True"}],
                    },
                }
            )
    emit({"items": pods})
elif len(args) >= 5 and args[0] == "-n" and args[2] == "get":
    key = resource_key(args[1], args[3])
    record = state["resources"].get(key)
    if record is None:
        print("NotFound", file=sys.stderr)
        raise SystemExit(1)
    emit(resource_object(record))
elif len(args) >= 5 and args[0] == "-n" and args[2] in ("scale", "patch"):
    namespace = args[1]
    ref = args[3]
    if args[2] == "scale":
        replicas_arg = next(row for row in args if row.startswith("--replicas="))
        replicas = int(replicas_arg.split("=", 1)[1])
    else:
        patch = json.loads(args[args.index("-p") + 1])
        replicas = int(patch["spec"]["replicas"])
    key = resource_key(namespace, ref)
    record = state["resources"].get(key)
    if record is None:
        print("NotFound", file=sys.stderr)
        raise SystemExit(1)
    record["spec"] = replicas
    record["ready"] = replicas
    record["available"] = replicas
    runtime_key = record.get("runtime_key")
    if runtime_key and runtime_key in state["resources"]:
        runtime = state["resources"][runtime_key]
        runtime["spec"] = replicas
        runtime["ready"] = replicas
        runtime["available"] = replicas
    save_state(state)
    emit(f"{ref} scaled")
else:
    print("unsupported fake oc command: oc " + " ".join(args), file=sys.stderr)
    raise SystemExit(97)
'''


FAKE_HELM = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
with Path(os.environ["FAKE_COMMAND_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write("helm " + " ".join(args) + "\n")

locks = {
    ("nvidia-vss-321-search", "nvidia-vss-search"): 7,
    ("vast-vss", "vast-vss"): 5,
    ("nims", "vss-cosmos-170"): 1,
    ("warehouse-demo", "nvidia-vss-warehouse"): 1,
}
namespace = args[args.index("-n") + 1]
pattern = args[args.index("--filter") + 1]
release = pattern.removeprefix("^").removesuffix("$")
revision = locks.get((namespace, release))
if revision is None:
    print("[]")
else:
    print(json.dumps([{"name": release, "revision": revision, "status": "deployed"}]))
'''


class DemoModeHarness:
    def __init__(self, *, resolved_warehouse: bool = False):
        self.temporary = tempfile.TemporaryDirectory(prefix="demo-mode-tests-")
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.scripts = self.root / "scripts"
        self.bin.mkdir()
        self.scripts.mkdir()
        shutil.copy2(SET_MODE, self.scripts / SET_MODE.name)
        shutil.copy2(SHOW_STATUS, self.scripts / SHOW_STATUS.name)
        self.profile = json.loads(PROFILES.read_text(encoding="utf-8"))
        self.profile["cluster"].update(
            {
                "expected_identity": "system:admin",
                "expected_api_server": "https://api.example.test:6443",
                "expected_context": "admin",
            }
        )
        if resolved_warehouse:
            warehouse = self.profile["profiles"]["warehouse"]
            warehouse["resolved"] = True
            warehouse["controller_inventory_complete"] = True
            warehouse["retained_data_plane_registered"] = True
            warehouse["locked_gpu_request"] = sum(
                int(row["replicas"]) * int(row["gpu_per_replica"])
                for row in warehouse["controllers"]
            )
            for row in warehouse["controllers"]:
                row["namespace"] = "warehouse-demo"
                row["resolved"] = True
            self.profile["retained_data_planes"].append(
                {
                    "namespace": "warehouse-demo",
                    "helm_release": "nvidia-vss-warehouse",
                    "verified_revision": 1,
                    "chart": "nvidia-warehouse-0.1.0",
                    "switch_behavior": "Retain all Warehouse state and PVCs.",
                }
            )
        (self.root / "profiles.yaml").write_text(
            json.dumps(self.profile, indent=2) + "\n", encoding="utf-8"
        )
        self._write_executable(self.bin / "oc", FAKE_OC)
        self._write_executable(self.bin / "helm", FAKE_HELM)
        self.state_path = self.root / "cluster-state.json"
        self.log_path = self.root / "commands.log"
        self.log_path.write_text("", encoding="utf-8")
        self.state = self._initial_state()
        self.save_state()

    def cleanup(self):
        self.temporary.cleanup()

    @staticmethod
    def _write_executable(path: Path, content: str):
        path.write_text(textwrap.dedent(content), encoding="utf-8")
        path.chmod(0o755)

    @staticmethod
    def _slug(item):
        return "-".join(
            [item["namespace"], item["runtime_kind"], item["runtime_name"]]
        ).lower().replace("_", "-")

    def _initial_state(self):
        resources = {}
        for profile_name, profile in self.profile["profiles"].items():
            replicas = 1 if profile_name == "base-cvd" else 0
            for item in profile["controllers"]:
                control_ref = f"{item['kind'].lower()}/{item['name']}"
                runtime_ref = f"{item['runtime_kind'].lower()}/{item['runtime_name']}"
                control_key = f"{item['namespace']}|{control_ref}"
                runtime_key = f"{item['namespace']}|{runtime_ref}"
                slug = self._slug(item)
                runtime_record = {
                    "namespace": item["namespace"],
                    "kind": item["runtime_kind"],
                    "name": item["runtime_name"],
                    "slug": slug,
                    "spec": replicas,
                    "ready": replicas,
                    "available": replicas,
                    "gpu": int(item["gpu_per_replica"]),
                    "runtime": True,
                    "retention": item.get("claim_retention_verified"),
                }
                resources[runtime_key] = runtime_record
                if control_key != runtime_key:
                    resources[control_key] = {
                        "namespace": item["namespace"],
                        "kind": item["kind"],
                        "name": item["name"],
                        "slug": slug,
                        "spec": replicas,
                        "ready": replicas,
                        "available": replicas,
                        "gpu": int(item["gpu_per_replica"]),
                        "runtime": False,
                        "runtime_key": runtime_key,
                    }
        return {
            "node_cordoned": True,
            "unrelated_gpu_pods": 0,
            "failure_injected": False,
            "resources": resources,
        }

    def save_state(self):
        self.state_path.write_text(
            json.dumps(self.state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def load_state(self):
        self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        return self.state

    def set_controller_replicas(self, item, replicas):
        control_key = f"{item['namespace']}|{item['kind'].lower()}/{item['name']}"
        runtime_key = (
            f"{item['namespace']}|{item['runtime_kind'].lower()}/{item['runtime_name']}"
        )
        for key in {control_key, runtime_key}:
            record = self.state["resources"][key]
            record["spec"] = replicas
            record["ready"] = replicas
            record["available"] = replicas
        self.save_state()

    def run(self, script, *arguments, fail_substring=""):
        env = os.environ.copy()
        env["PATH"] = str(self.bin) + os.pathsep + env["PATH"]
        env["FAKE_CLUSTER_STATE"] = str(self.state_path)
        env["FAKE_COMMAND_LOG"] = str(self.log_path)
        if fail_substring:
            env["FAKE_FAIL_SUBSTRING"] = fail_substring
        return subprocess.run(
            [str(self.scripts / script), *arguments],
            text=True,
            capture_output=True,
            env=env,
            cwd=self.root,
            timeout=15,
            check=False,
        )

    def commands(self):
        return self.log_path.read_text(encoding="utf-8").splitlines()

    def mutation_commands(self):
        return [
            row
            for row in self.commands()
            if " patch " in f" {row} "
            or " scale " in f" {row} "
            or row.startswith("oc adm ")
        ]


class DemoModeSwitchingTests(unittest.TestCase):
    def setUp(self):
        self.harnesses = []

    def tearDown(self):
        for harness in self.harnesses:
            harness.cleanup()

    def harness(self, *, resolved_warehouse=False):
        harness = DemoModeHarness(resolved_warehouse=resolved_warehouse)
        self.harnesses.append(harness)
        return harness

    def test_plan_only_reports_unresolved_gate_without_mutation(self):
        harness = self.harness()
        result = harness.run("set-demo-mode.sh", "warehouse")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PLAN ONLY", result.stdout)
        self.assertIn("EXECUTION BLOCKED", result.stdout)
        self.assertEqual(harness.mutation_commands(), [])

    def test_execute_rejects_unresolved_warehouse_before_mutation(self):
        harness = self.harness()
        result = harness.run(
            "set-demo-mode.sh", "warehouse", "--execute", "--ack", ACK
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("both profile controller inventories must be resolved", result.stderr)
        self.assertEqual(harness.mutation_commands(), [])

    def test_pre_mutation_capacity_rejection_does_not_change_replicas(self):
        harness = self.harness(resolved_warehouse=True)
        harness.state["unrelated_gpu_pods"] = 1
        harness.save_state()
        result = harness.run(
            "set-demo-mode.sh", "warehouse", "--execute", "--ack", ACK
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected non-source GPU requests=1", result.stderr)
        self.assertEqual(harness.mutation_commands(), [])
        self.assertTrue(harness.load_state()["node_cordoned"])

    def test_partial_target_failure_restores_base_and_re_cordons(self):
        harness = self.harness(resolved_warehouse=True)
        failure = "scale deployment/warehouse-nemotron-nano"
        result = harness.run(
            "set-demo-mode.sh",
            "warehouse",
            "--execute",
            "--ack",
            ACK,
            fail_substring=failure,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RECOVERY", result.stderr)
        self.assertIn("recorded base-cvd was restored", result.stderr)
        state = harness.load_state()
        self.assertTrue(state["failure_injected"])
        self.assertTrue(state["node_cordoned"])
        for item in harness.profile["profiles"]["base-cvd"]["controllers"]:
            control = (
                f"{item['namespace']}|{item['kind'].lower()}/{item['name']}"
            )
            runtime = (
                f"{item['namespace']}|"
                f"{item['runtime_kind'].lower()}/{item['runtime_name']}"
            )
            self.assertEqual(state["resources"][control]["spec"], 1)
            self.assertEqual(state["resources"][runtime]["ready"], 1)
        for item in harness.profile["profiles"]["warehouse"]["controllers"]:
            control = (
                f"{item['namespace']}|{item['kind'].lower()}/{item['name']}"
            )
            runtime = (
                f"{item['namespace']}|"
                f"{item['runtime_kind'].lower()}/{item['runtime_name']}"
            )
            self.assertEqual(state["resources"][control]["spec"], 0)
            self.assertEqual(state["resources"][runtime]["ready"], 0)
        commands = harness.commands()
        uncordons = [index for index, row in enumerate(commands) if row == "oc adm uncordon gpu-worker"]
        cordons = [index for index, row in enumerate(commands) if row == "oc adm cordon gpu-worker"]
        self.assertGreaterEqual(len(uncordons), 2)
        self.assertGreaterEqual(len(cordons), 2)
        self.assertGreater(cordons[-1], uncordons[-1])

    def test_show_status_returns_nonzero_when_node_is_uncordoned(self):
        harness = self.harness()
        harness.state["node_cordoned"] = False
        harness.save_state()
        result = harness.run("show-status.sh")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gpu-worker end-state gate: FAIL", result.stdout)
        self.assertIn("unsafe or incomplete profile state", result.stderr)
        self.assertEqual(harness.mutation_commands(), [])

    def test_show_status_returns_nonzero_for_mixed_profile(self):
        harness = self.harness()
        first = harness.profile["profiles"]["base-cvd"]["controllers"][0]
        harness.set_controller_replicas(first, 0)
        result = harness.run("show-status.sh")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Inferred mode: mixed-or-transitioning", result.stdout)
        self.assertIn("unsafe or incomplete profile state", result.stderr)
        self.assertEqual(harness.mutation_commands(), [])

    def test_show_status_returns_nonzero_when_gpu_requests_exceed_capacity(self):
        harness = self.harness()
        harness.state["unrelated_gpu_pods"] = 1
        harness.save_state()
        result = harness.run("show-status.sh")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GPU requests: 9/8", result.stdout)
        self.assertIn("GPU capacity gate: FAIL", result.stdout)
        self.assertEqual(harness.mutation_commands(), [])


if __name__ == "__main__":
    unittest.main()
