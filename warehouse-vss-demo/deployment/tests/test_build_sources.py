"""Check the standalone build closure without downloads, Docker or a cluster."""
import hashlib
import importlib.util
import json
import subprocess
import unittest
from pathlib import Path

BUILD = Path(__file__).resolve().parents[1] / "build"


class BuildSourceTests(unittest.TestCase):
    def test_all_locked_local_dockerfiles_are_present_and_unchanged(self):
        lock = json.loads((BUILD / "derived-images.lock.json").read_text())
        self.assertEqual(len(lock["buildFiles"]), 6)
        for relative, expected in lock["buildFiles"].items():
            self.assertEqual(hashlib.sha256((BUILD / relative).read_bytes()).hexdigest(), expected)

    def test_selected_kafka_image_is_the_corrected_random_uid_build(self):
        lock = json.loads((BUILD / "derived-images.lock.json").read_text())
        kafka = next(item for item in lock["outputs"] if item["key"] == "kafka-health-check")
        self.assertEqual(kafka["outputDigest"], kafka["replacement"]["registryDigest"])
        self.assertEqual(kafka["outputDockerfileSha256"], lock["buildFiles"]["dockerfiles/Dockerfile.kafka-health-check"])

    def test_relocated_python_builders_use_the_shipped_build_directory(self):
        for filename, variable in (("prepare-derived-build-contexts.py", "BUILD_DIR"), ("fetch-derived-image-artifacts.py", "LOCK_FILE")):
            spec = importlib.util.spec_from_file_location(filename, BUILD / "scripts" / filename)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            actual = getattr(module, variable)
            self.assertEqual(actual, BUILD if variable == "BUILD_DIR" else BUILD / "derived-images.lock.json")

    def test_all_shell_builders_pass_syntax_without_running(self):
        for path in (BUILD / "scripts").glob("*.sh"):
            subprocess.run(["bash", "-n", str(path)], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
