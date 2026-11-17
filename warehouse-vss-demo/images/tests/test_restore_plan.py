"""Offline restore planning and registry-only verification contracts."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import make_restore_plan as restore


class RestoreTests(unittest.TestCase):
    def inventory(self):
        return json.loads((ROOT / "required-images.json").read_text())

    def test_all_references_and_shared_busybox_platform_are_retained(self):
        plan = restore.generate(self.inventory(), "registry.customer.example/warehouse")
        self.assertEqual(len(plan["mappings"]), 24)
        self.assertEqual(len(plan["expectedImageOverrides"]), 25)
        self.assertTrue(all(x.startswith("file://warehouse-preserved/") for x in plan["mappings"]))
        self.assertTrue(all("@sha256:" in x for x in plan["expectedImageOverrides"].values()))
        self.assertFalse(plan["registryWrite"])

    def test_unsafe_registry_prefix_rejected(self):
        for prefix in ("https://registry.example/org", "registry.example/org/", "u:p@registry.example/org", "registry.example/a/../b", "registry.example/org\n"):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                restore.generate(self.inventory(), prefix)

    def test_plan_does_not_contact_a_registry_or_overwrite(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(restore.subprocess, "run") as runner:
            target = Path(directory) / "new-plan"
            restore.write_plan(self.inventory(), "registry.customer.example/warehouse", target)
            runner.assert_not_called()
            with self.assertRaises(ValueError):
                restore.write_plan(self.inventory(), "registry.customer.example/warehouse", target)

    def test_all_targets_verified_before_any_overrides_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "new-plan"
            restore.write_plan(self.inventory(), "registry.customer.example/warehouse", target)
            calls = []
            def runner(args, **kwargs):
                calls.append(args)
                pin = args[3]
                return SimpleNamespace(returncode=0, stdout=json.dumps({"digest": pin.rsplit("@", 1)[1], "config": {"os": "linux", "architecture": "amd64"}}))
            result = restore.verify_plan(target, runner=runner)
            self.assertEqual(len(calls), 24)
            self.assertEqual(result["sourcePins"], 25)
            self.assertTrue(all(x[:3] == ["oc", "image", "info"] for x in calls))
            self.assertEqual(len(json.loads((target / "verified-image-overrides.json").read_text())), 25)

    def test_tampered_mapping_and_missing_target_emit_no_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "new-plan"
            restore.write_plan(self.inventory(), "registry.customer.example/warehouse", target)
            with self.assertRaises(ValueError):
                restore.verify_plan(target, runner=lambda *a, **kw: SimpleNamespace(returncode=1))
            self.assertFalse((target / "verified-image-overrides.json").exists())
            (target / "mirror-inputs.txt").write_text("unreviewed\n")
            with mock.patch.object(restore.subprocess, "run") as runner, self.assertRaises(ValueError):
                restore.verify_plan(target, runner=runner)
            runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
