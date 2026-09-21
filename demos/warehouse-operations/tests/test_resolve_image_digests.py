"""Offline tests for the Warehouse image-digest resolver.

Every subprocess test places a purpose-built fake ``oc`` executable first in
``PATH``. No command can discover or contact an OpenShift cluster or registry.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "demos" / "warehouse-operations" / "scripts" / "resolve-image-digests.py"


def load_resolver():
    spec = importlib.util.spec_from_file_location("warehouse_digest_resolver", SCRIPT)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FAKE_OC = r'''#!/usr/bin/env python3
import json
import sys

args = sys.argv[1:]
if args[:2] != ["image", "info"] or args[-2:] != ["-o", "json"]:
    print("unexpected fake oc invocation", file=sys.stderr)
    raise SystemExit(97)

reference = args[2]
if reference.endswith("/missing/image:1"):
    print("fake registry: manifest unknown", file=sys.stderr)
    raise SystemExit(1)
if reference.endswith("/no-digest/image:1"):
    print(json.dumps({"name": reference}))
    raise SystemExit(0)
if reference.endswith("/single/image:1"):
    print(json.dumps({
        "name": reference,
        "digest": "sha256:" + "2" * 64,
    }))
    raise SystemExit(0)

print(json.dumps({
    "name": reference,
    "listDigest": "sha256:" + "1" * 64,
    "digest": "sha256:" + "3" * 64,
}))
'''


class ResolveImageDigestsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.resolver = load_resolver()

    def test_read_images_stops_at_next_top_level_key(self):
        with tempfile.TemporaryDirectory(prefix="warehouse-digest-parse-") as tmp:
            source_lock = Path(tmp) / "source-lock.yaml"
            source_lock.write_text(
                textwrap.dedent(
                    """\
                    lock_version: "1.0"
                    images:
                      - redis:8.6.2-alpine
                      - nvcr.io/nvidia/vss-core/vss-agent:3.2.1
                    image_digest_resolution:
                      unresolved:
                        - must-not-be-read:1
                    """
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                self.resolver.read_images(source_lock),
                [
                    "redis:8.6.2-alpine",
                    "nvcr.io/nvidia/vss-core/vss-agent:3.2.1",
                ],
            )

    def test_read_images_rejects_missing_or_empty_images_section(self):
        with tempfile.TemporaryDirectory(prefix="warehouse-digest-empty-") as tmp:
            path = Path(tmp) / "source-lock.yaml"
            for content in ("source: {}\n", "images:\nnext: value\n"):
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "no images found under images"):
                        self.resolver.read_images(path)

    def test_reference_normalization_matches_registry_rules(self):
        cases = {
            "redis:8.6.2-alpine": "docker.io/library/redis:8.6.2-alpine",
            "nvidia/dcgm-exporter:3.3.6": "docker.io/nvidia/dcgm-exporter:3.3.6",
            "nvcr.io/nvidia/vss-core/vss-agent:3.2.1": "nvcr.io/nvidia/vss-core/vss-agent:3.2.1",
            "localhost:5000/team/image:1": "localhost:5000/team/image:1",
            "registry.example.com:5000/team/image:1": "registry.example.com:5000/team/image:1",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(self.resolver.normalize_reference(source), expected)

    def test_resolve_uses_fake_oc_and_prefers_manifest_list_digest(self):
        with self.fake_oc_environment() as env:
            old_path = os.environ.get("PATH")
            os.environ["PATH"] = env["PATH"]
            try:
                result = self.resolver.resolve("redis:8.6.2-alpine", "linux/amd64")
            finally:
                if old_path is None:
                    os.environ.pop("PATH", None)
                else:
                    os.environ["PATH"] = old_path

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["normalized"], "docker.io/library/redis:8.6.2-alpine")
        self.assertEqual(result["digest"], "sha256:" + "1" * 64)
        self.assertEqual(result["platformDigest"], "sha256:" + "3" * 64)
        self.assertEqual(result["manifestListDigest"], "sha256:" + "1" * 64)

    def test_resolve_passes_registry_config_without_reading_it(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "digest": "sha256:" + "4" * 64,
                }
            ),
            stderr="",
        )
        with mock.patch.object(
            self.resolver.subprocess,
            "run",
            return_value=completed,
        ) as run:
            result = self.resolver.resolve(
                "nvcr.io/nvidia/vss-core/vss-rt-vlm:3.2.1",
                "linux/amd64",
                "/tmp/temporary-auth/config.json",
            )

        command = run.call_args.args[0]
        self.assertIn(
            "--registry-config=/tmp/temporary-auth/config.json",
            command,
        )
        self.assertEqual(result["status"], "resolved")

    def test_cli_reports_resolved_and_failed_without_real_cluster_access(self):
        with tempfile.TemporaryDirectory(prefix="warehouse-digest-cli-") as tmp:
            root = Path(tmp)
            source_lock = root / "source-lock.yaml"
            output = root / "result.json"
            source_lock.write_text(
                textwrap.dedent(
                    """\
                    images:
                      - redis:8.6.2-alpine
                      - single/image:1
                      - missing/image:1
                    trailing_section:
                      - ignored/image:1
                    """
                ),
                encoding="utf-8",
            )
            with self.fake_oc_environment() as env:
                completed = subprocess.run(
                    [
                        "python3",
                        str(SCRIPT),
                        "--source-lock",
                        str(source_lock),
                        "--platform",
                        "linux/amd64",
                        "--output",
                        str(output),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=env,
                )

            self.assertEqual(completed.returncode, 1)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["resolved"], 2)
            self.assertEqual(payload["failed"], 1)
            self.assertEqual(len(payload["images"]), 3)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), payload)
            self.assertEqual(payload["images"][2]["error"], "fake registry: manifest unknown")

    def test_cli_can_limit_resolution_to_a_source_locked_image(self):
        with tempfile.TemporaryDirectory(prefix="warehouse-digest-only-") as tmp:
            root = Path(tmp)
            source_lock = root / "source-lock.yaml"
            registry_config = root / "config.json"
            source_lock.write_text(
                "images:\n  - redis:8.6.2-alpine\n  - single/image:1\n",
                encoding="utf-8",
            )
            registry_config.write_text("{}\n", encoding="utf-8")
            with self.fake_oc_environment() as env:
                completed = subprocess.run(
                    [
                        "python3",
                        str(SCRIPT),
                        "--source-lock",
                        str(source_lock),
                        "--image",
                        "single/image:1",
                        "--registry-config",
                        str(registry_config),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=env,
                )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual([item["source"] for item in payload["images"]], ["single/image:1"])

    def test_cli_rejects_an_image_outside_the_source_lock(self):
        with tempfile.TemporaryDirectory(prefix="warehouse-digest-reject-") as tmp:
            source_lock = Path(tmp) / "source-lock.yaml"
            source_lock.write_text(
                "images:\n  - redis:8.6.2-alpine\n",
                encoding="utf-8",
            )
            with self.fake_oc_environment() as env:
                completed = subprocess.run(
                    [
                        "python3",
                        str(SCRIPT),
                        "--source-lock",
                        str(source_lock),
                        "--image",
                        "outside/image:1",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=env,
                )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("requested image is not in the source lock", completed.stderr)

    def test_missing_digest_is_reported_as_an_error(self):
        with self.fake_oc_environment() as env:
            old_path = os.environ.get("PATH")
            os.environ["PATH"] = env["PATH"]
            try:
                result = self.resolver.resolve("no-digest/image:1", "linux/amd64")
            finally:
                if old_path is None:
                    os.environ.pop("PATH", None)
                else:
                    os.environ["PATH"] = old_path

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "registry metadata did not include a digest")

    class fake_oc_environment:
        def __enter__(self):
            self.temporary = tempfile.TemporaryDirectory(prefix="warehouse-fake-oc-")
            root = Path(self.temporary.name)
            executable = root / "oc"
            executable.write_text(textwrap.dedent(FAKE_OC), encoding="utf-8")
            executable.chmod(0o755)
            self.environment = os.environ.copy()
            self.environment["PATH"] = f"{root}{os.pathsep}{self.environment.get('PATH', '')}"
            return self.environment

        def __exit__(self, exc_type, exc_value, traceback):
            self.temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
