"""Tiny offline fixtures for native registry-v2 file mirror verification."""

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "verify_file_mirror.py"
SPEC = importlib.util.spec_from_file_location("verify_file_mirror", SCRIPT)
mirror = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mirror)


def encoded(value):
    return json.dumps(value, sort_keys=True).encode()


def checksum(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="warehouse-file-mirror-test-")
        self.root = Path(self.temporary.name) / "mirror"
        self.repository = self.root / "v2" / "warehouse-preserved" / "example"
        (self.repository / "blobs").mkdir(parents=True)
        (self.repository / "manifests").mkdir()
        self.config = {"os": "linux", "architecture": "amd64",
                       "config": {"Env": ["DO_NOT_PRINT_IMAGE_ENV=private-marker"]},
                       "history": [{"created_by": "DO_NOT_PRINT_IMAGE_HISTORY"}]}
        self.make_image(self.config)

    def tearDown(self):
        self.temporary.cleanup()

    def blob(self, raw):
        digest = checksum(raw)
        (self.repository / "blobs" / digest).write_bytes(raw)
        return {"digest": digest, "size": len(raw)}

    def make_image(self, config):
        self.config_descriptor = self.blob(encoded(config))
        self.config_descriptor["mediaType"] = "application/vnd.oci.image.config.v1+json"
        self.layer_descriptor = self.blob(b"tiny-layer-payload")
        self.layer_descriptor["mediaType"] = "application/vnd.oci.image.layer.v1.tar"
        self.manifest = {"schemaVersion": 2,
                         "mediaType": "application/vnd.oci.image.manifest.v1+json",
                         "config": self.config_descriptor, "layers": [self.layer_descriptor]}
        self.bind_manifest()

    def bind_manifest(self):
        raw = encoded(self.manifest)
        self.digest = checksum(raw)
        self.manifest_path = self.repository / "manifests" / self.digest
        self.manifest_path.write_bytes(raw)
        self.blob(raw)  # Native mirrors can duplicate manifest content here.
        self.tag = self.repository / "manifests" / "linux-amd64"
        if self.tag.exists() or self.tag.is_symlink():
            self.tag.unlink()
        self.tag.symlink_to(self.digest)
        self.inventory = {"format": 1, "platform": "linux/amd64", "images": [{
            "packagePin": "registry.example.org/example@sha256:" + "a" * 64,
            "platformDigest": self.digest, "fileRepository": "warehouse-preserved/example"}]}

    def verify(self):
        return mirror.verify_file_mirror(self.root, self.inventory)

    def test_valid_mirror_hashes_all_content_and_deduplicates_package_rows(self):
        second = copy.deepcopy(self.inventory["images"][0])
        second["packagePin"] = "registry.example.org/example@sha256:" + "b" * 64
        self.inventory["images"].append(second)
        summary = self.verify()
        self.assertTrue(summary["contentValidated"])
        self.assertEqual(summary["imageCounts"], {
            "packagePins": 2, "uniquePlatformImages": 1, "repositoryManifests": 1})
        self.assertEqual(summary["filesValidated"], 4)
        self.assertEqual(summary["bytesValidated"], sum(p.stat().st_size for p in self.repository.rglob("*")
                                                       if p.is_file() and not p.is_symlink()))

    def test_changed_layer_fails_even_when_size_is_unchanged(self):
        path = self.repository / "blobs" / self.layer_descriptor["digest"]
        path.write_bytes(b"X" * path.stat().st_size)
        with self.assertRaisesRegex(mirror.VerificationError, "checksum mismatch"):
            self.verify()

    def test_twenty_five_pins_can_bind_twenty_four_unique_platform_images(self):
        rows = []
        for index in range(24):
            config = dict(self.config, fixtureIndex=index)
            self.make_image(config)
            row = copy.deepcopy(self.inventory["images"][0])
            row["packagePin"] = "registry.example.org/example@sha256:" + f"{index:064x}"
            rows.append(row)
        alias = copy.deepcopy(rows[0])
        alias["packagePin"] = "registry.example.org/example@sha256:" + f"{24:064x}"
        self.inventory["images"] = rows + [alias]
        summary = self.verify()
        self.assertEqual(summary["imageCounts"], {
            "packagePins": 25, "uniquePlatformImages": 24, "repositoryManifests": 24})

    def test_each_repository_must_contain_its_own_referenced_blobs(self):
        other = self.repository.with_name("other")
        shutil.copytree(self.repository, other, symlinks=True)
        row = copy.deepcopy(self.inventory["images"][0])
        row.update(packagePin="registry.example.org/other@sha256:" + "b" * 64,
                   fileRepository="warehouse-preserved/other")
        self.inventory["images"].append(row)
        (other / "blobs" / self.layer_descriptor["digest"]).unlink()
        with self.assertRaisesRegex(mirror.VerificationError, "Referenced image blob is missing"):
            self.verify()

    def test_unreferenced_content_addressed_blob_is_also_hashed(self):
        (self.repository / "blobs" / ("sha256:" + "c" * 64)).write_bytes(b"not-the-named-digest")
        with self.assertRaisesRegex(mirror.VerificationError, "checksum mismatch"):
            self.verify()

    def test_duplicate_manifest_blob_is_independently_hashed(self):
        (self.repository / "blobs" / self.digest).write_bytes(b"tampered duplicate")
        with self.assertRaisesRegex(mirror.VerificationError, "checksum mismatch"):
            self.verify()

    def test_missing_config_layer_or_manifest_fails(self):
        for kind in ("config", "layer", "manifest"):
            with self.subTest(kind=kind):
                if kind == "config":
                    path = self.repository / "blobs" / self.config_descriptor["digest"]
                elif kind == "layer":
                    path = self.repository / "blobs" / self.layer_descriptor["digest"]
                else:
                    path = self.manifest_path
                    self.tag.unlink()
                raw = path.read_bytes()
                path.unlink()
                with self.assertRaisesRegex(mirror.VerificationError, "missing"):
                    self.verify()
                path.write_bytes(raw)
                if kind == "manifest":
                    self.tag.symlink_to(self.digest)

    def test_descriptor_size_must_match_blob(self):
        self.manifest["layers"][0]["size"] += 1
        self.bind_manifest()
        with self.assertRaisesRegex(mirror.VerificationError, "size differs"):
            self.verify()

    def test_wrong_os_or_architecture_is_rejected_without_config_disclosure(self):
        for field, value in (("os", "windows"), ("architecture", "arm64")):
            with self.subTest(field=field):
                config = copy.deepcopy(self.config)
                config[field] = value
                self.make_image(config)
                with self.assertRaisesRegex(mirror.VerificationError, "not linux/amd64") as error:
                    self.verify()
                self.assertNotIn("private-marker", str(error.exception))
                self.assertNotIn("DO_NOT_PRINT_IMAGE_HISTORY", str(error.exception))

    def test_platform_digest_cannot_reference_a_manifest_index(self):
        self.manifest = {"schemaVersion": 2,
                         "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": []}
        self.bind_manifest()
        with self.assertRaisesRegex(mirror.VerificationError, "leaf image manifest"):
            self.verify()

    def test_escape_or_dangling_tag_is_rejected(self):
        outside = Path(self.temporary.name) / "outside"
        outside.write_bytes(self.manifest_path.read_bytes())
        self.tag.unlink()
        self.tag.symlink_to(outside)
        with self.assertRaisesRegex(mirror.VerificationError, "escapes"):
            self.verify()
        self.tag.unlink()
        self.tag.symlink_to("sha256:" + "d" * 64)
        with self.assertRaises(OSError):
            self.verify()

    def test_blob_or_directory_symlink_is_not_followed(self):
        path = self.repository / "blobs" / self.layer_descriptor["digest"]
        raw = path.read_bytes()
        path.unlink()
        path.symlink_to(self.manifest_path)
        with self.assertRaisesRegex(mirror.VerificationError, "Only manifest tags"):
            self.verify()
        path.unlink()
        path.write_bytes(raw)
        (self.root / "linked-directory").symlink_to(self.repository, target_is_directory=True)
        with self.assertRaises(mirror.VerificationError):
            self.verify()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO fixture requires POSIX")
    def test_special_file_is_rejected_without_opening_it(self):
        os.mkfifo(self.repository / "blobs" / ("sha256:" + "e" * 64))
        with self.assertRaisesRegex(mirror.VerificationError, "Special files"):
            self.verify()

    def test_unsafe_inventory_repository_or_digest_is_rejected(self):
        for value in ("../outside", "/absolute", "one//two", "one/../two", "one\\two", "UPPER/name"):
            with self.subTest(value=value):
                self.inventory["images"][0]["fileRepository"] = value
                with self.assertRaises(mirror.VerificationError):
                    self.verify()
        self.inventory["images"][0]["fileRepository"] = "warehouse-preserved/example"
        self.inventory["images"][0]["platformDigest"] = "../../outside"
        with self.assertRaises(mirror.VerificationError):
            self.verify()

    def test_duplicate_package_pin_or_wrong_platform_is_rejected(self):
        self.inventory["images"].append(copy.deepcopy(self.inventory["images"][0]))
        with self.assertRaisesRegex(mirror.VerificationError, "Duplicate"):
            self.verify()
        self.inventory["images"].pop()
        self.inventory["platform"] = "linux/arm64"
        with self.assertRaisesRegex(mirror.VerificationError, "linux/amd64"):
            self.verify()

    def test_hardlinked_duplicate_content_can_reuse_inode_hash(self):
        duplicate = self.repository / "blobs" / self.digest
        duplicate.unlink()
        os.link(self.manifest_path, duplicate)
        summary = self.verify()
        self.assertEqual(summary["bytesValidated"] - summary["bytesHashed"], self.manifest_path.stat().st_size)

    def test_regular_manifest_tag_is_checked_against_its_digest(self):
        self.tag.unlink()
        self.tag.write_bytes(self.manifest_path.read_bytes())
        self.assertTrue(self.verify()["contentValidated"])

    def test_cli_outputs_compact_summary_without_configuration_content(self):
        inventory = Path(self.temporary.name) / "inventory.json"
        inventory.write_text(json.dumps(self.inventory))
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--directory", str(self.root),
                                 "--inventory", str(inventory)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["contentValidated"])
        self.assertEqual(result.stdout.count("\n"), 1)
        self.assertNotIn("private-marker", result.stdout + result.stderr)
        self.assertNotIn("DO_NOT_PRINT_IMAGE_HISTORY", result.stdout + result.stderr)

    def test_cli_failure_does_not_disclose_configuration_content(self):
        self.make_image(dict(self.config, architecture="arm64"))
        inventory = Path(self.temporary.name) / "inventory.json"
        inventory.write_text(json.dumps(self.inventory))
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--directory", str(self.root),
                                 "--inventory", str(inventory)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stderr)["contentValidated"])
        self.assertEqual(result.stdout, "")
        self.assertNotIn("private-marker", result.stderr)
        self.assertNotIn("DO_NOT_PRINT_IMAGE_HISTORY", result.stderr)


if __name__ == "__main__":
    unittest.main()
