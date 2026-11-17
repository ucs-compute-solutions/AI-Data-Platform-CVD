"""Source integrity and standalone document-link regressions."""
import importlib.util
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "verify.py"
SPEC = importlib.util.spec_from_file_location("warehouse_verify", MODULE)
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


class SourceChecks(unittest.TestCase):
    def make_manifest(self, root):
        source = root / "README.md"
        source.write_text("source\n")
        manifest = {"format": 1, "published": False, "freshInstallAccepted": False,
                    "portableHourLifecycleAccepted": False,
                    "files": {source.name: verify.file_entry(source)}}
        (root / verify.MANIFEST).write_text(json.dumps(manifest))

    def test_extracted_package_works_without_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_manifest(root)
            with patch.object(verify.subprocess, "run", side_effect=FileNotFoundError):
                verify.check_manifest(root)
                (root / "extra.sh").write_text("exit 0\n")
                with self.assertRaisesRegex(ValueError, "inventory"):
                    verify.check_manifest(root)

    def test_unlisted_sources_and_symlinks_rejected(self):
        for name in ("extra.py", "nested/extra.sh", "data.bin", "local.env", "linked",
                     "__pycache__/extra.py", "__pycache__/local.env"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.make_manifest(root)
                source = root / name
                source.parent.mkdir(parents=True, exist_ok=True)
                if name == "linked":
                    source.symlink_to(root / "README.md")
                else:
                    source.write_bytes(b"synthetic extra payload\n")
                with self.assertRaisesRegex(ValueError, "inventory"):
                    verify.check_manifest(root)

    def test_git_ignored_tracked_file_cannot_escape_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            self.make_manifest(root)
            hidden = root / "private.env"
            hidden.write_text("synthetic fixture\n")
            (root / ".gitignore").write_text("*.env\n.gitignore\n")
            subprocess.run(["git", "-C", str(root), "add", "-f", "private.env"], check=True)
            with self.assertRaisesRegex(ValueError, "inventory"):
                verify.check_manifest(root)

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

    def test_credential_scan_covers_shell_sql_and_embedded_json(self):
        value = secrets.token_hex(16)
        script = "PG_APP_" + "PASSWORD='" + value + "'\n"
        samples = {"script.sh": script,
                   "init.sql": "ALTER ROLE demo PASSWORD '" + value + "';\n",
                   "capture.json": json.dumps({"data": {"bootstrap.sh": script}})}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, content in samples.items():
                (root / name).write_text(content)
                with self.subTest(name=name), self.assertRaises(ValueError) as error:
                    verify.check_content(root, [name])
                self.assertNotIn(value, str(error.exception))

    def test_actual_runtime_password_file_is_not_a_literal(self):
        files = ["deployment/fresh-postgres-bootstrap.sh",
                 "deployment/baseline/baseline.captured.json"]
        result = verify.check_content(verify.ROOT, files)
        self.assertEqual(result, {"python": 0, "json": 1, "bash": 1})


if __name__ == "__main__":
    unittest.main()
