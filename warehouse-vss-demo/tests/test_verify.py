"""Source integrity and standalone document-link regressions."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / "verify.py"
SPEC = importlib.util.spec_from_file_location("warehouse_verify", MODULE)
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


class SourceChecks(unittest.TestCase):
    def test_absolute_traversal_and_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("../outside", "/outside", "a/../b", "a\\b", "", "./a"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    verify.source_path(root, name)
            (root / "linked").symlink_to(MODULE)
            with self.assertRaises(ValueError):
                verify.source_path(root, "linked")

    def test_changed_missing_and_status_tamper_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "README.md"
            source.write_text("source\n")
            manifest = {"format": 1, "published": False, "freshInstallAccepted": False,
                        "portableHourLifecycleAccepted": False,
                        "files": {source.name: verify.file_entry(source)}}
            destination = root / verify.MANIFEST
            destination.write_text(json.dumps(manifest))
            self.assertEqual(verify.check_manifest(root), manifest)
            source.write_text("changed\n")
            with self.assertRaisesRegex(ValueError, "changed"):
                verify.check_manifest(root)
            source.unlink()
            with self.assertRaisesRegex(ValueError, "missing"):
                verify.check_manifest(root)
            manifest["freshInstallAccepted"] = True
            destination.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "status"):
                verify.check_manifest(root)

    def test_local_links_must_stay_in_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "README.md"
            (root / "target.md").write_text("# Target\n")
            source.write_text("[local](target.md#target) [external](https://example.com)\n")
            self.assertEqual(verify.check_links(root, [source.name]), 1)
            for target in ("../outside.md", "missing.md", "/outside.md", "javascript:alert"):
                source.write_text(f"[bad]({target})\n")
                with self.subTest(target=target), self.assertRaises(ValueError):
                    verify.check_links(root, [source.name])

    def test_non_finite_json_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            path.write_text('{"value": NaN}')
            with self.assertRaises(ValueError):
                verify.load_json(path)


if __name__ == "__main__":
    unittest.main()
