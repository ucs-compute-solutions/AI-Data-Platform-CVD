"""Synthetic Docker-save/OCI archive integrity tests; no engines or network."""
import contextlib
import copy
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "verify_docker_archive.py"
SPEC = importlib.util.spec_from_file_location("verify_docker_archive", SOURCE)
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def layer_bytes(label):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        content = label.encode()
        member = tarfile.TarInfo("example.txt")
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


class Fixture:
    def __init__(self, count=1, *, platform="amd64", compressed=False, legacy=False, bad_diffid=False):
        self.files, self.entries, self.descriptors, self.records = {}, [], [], []
        for number in range(count):
            raw_layer = layer_bytes("image-" + str(number))
            layer = gzip.compress(raw_layer, mtime=0) if compressed else raw_layer
            diffid = "sha256:" + ("0" * 64 if bad_diffid else digest(raw_layer))
            config = encode({"os": "linux", "architecture": platform,
                             "rootfs": {"type": "layers", "diff_ids": [diffid]},
                             "config": {"Env": ["EXAMPLE=must-not-appear-in-summary"]}})
            ident = "sha256:" + digest(config)
            config_name = digest(config) + ".json" if legacy else "blobs/sha256/" + digest(config)
            layer_name = "layer-" + str(number) + "/layer.tar" if legacy else "blobs/sha256/" + digest(layer)
            self.files[config_name], self.files[layer_name] = config, layer
            self.entries.append({"Config": config_name, "RepoTags": None, "Layers": [layer_name]})
            self.records.append({"source": "registry.example.org:5000/example/image-" + str(number) + "@sha256:" + str(number + 1) * 64,
                                 "imageId": ident})
            if not legacy:
                image = encode({"schemaVersion": 2,
                    "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": ident, "size": len(config)},
                    "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip" if compressed else "application/vnd.oci.image.layer.v1.tar",
                                "digest": "sha256:" + digest(layer), "size": len(layer)}]})
                self.files["blobs/sha256/" + digest(image)] = image
                self.descriptors.append({"mediaType": "application/vnd.oci.image.manifest.v1+json",
                                         "digest": "sha256:" + digest(image), "size": len(image)})
        self.files["manifest.json"] = encode(self.entries)

    @property
    def sidecar(self):
        return {"images": copy.deepcopy(self.records)}

    def oci(self, descriptors=None):
        self.files["oci-layout"] = encode({"imageLayoutVersion": "1.0.0"})
        self.files["index.json"] = encode({"schemaVersion": 2,
                                           "manifests": self.descriptors if descriptors is None else descriptors})
        return self

    def write(self, path, extra=()):
        with tarfile.open(path, "w") as archive:
            for name, value in self.files.items():
                member = tarfile.TarInfo(name)
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))
            for member, value in extra:
                archive.addfile(member, io.BytesIO(value) if value is not None else None)
        return path


class DockerArchiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.archive = Path(self.directory.name) / "images.tar"

    def check(self, fixture, sidecar=None, extra=()):
        fixture.write(self.archive, extra)
        return verifier.verify_archive(self.archive, fixture.sidecar if sidecar is None else sidecar)

    def test_all_docker_entries_are_verified_with_partial_oci_index(self):
        fixture = Fixture(2).oci()
        fixture.oci(fixture.descriptors[:1])
        result = self.check(fixture)
        self.assertEqual(result["dockerImages"], 2)
        self.assertEqual(result["ociIndexedImages"], 1)
        self.assertEqual(result["restoreMap"], fixture.records)
        self.assertEqual(result["platform"], "linux/amd64")
        self.assertFalse(result["engineLoadTested"])
        self.assertNotIn("must-not-appear-in-summary", json.dumps(result))
        self.assertNotIn(str(self.directory.name), json.dumps(result))

    def test_legacy_docker_save_config_names_and_layer_paths(self):
        result = self.check(Fixture(2, legacy=True))
        self.assertEqual(result["dockerImages"], 2)
        self.assertEqual(result["dockerLayerReferences"], 2)

    def test_gzip_layer_is_verified_against_uncompressed_diffid(self):
        self.assertEqual(self.check(Fixture(compressed=True).oci())["dockerImages"], 1)

    def test_sidecar_is_mandatory_and_records_must_be_explicit(self):
        for sidecar in ({}, {"images": []}, {"images": 2}, {"images": [{"source": "example"}]}):
            with self.subTest(sidecar=sidecar), self.assertRaises(verifier.Invalid):
                self.check(Fixture(), sidecar)

    def test_duplicate_or_credential_bearing_sidecar_source_is_rejected(self):
        fixture = Fixture()
        duplicate = fixture.sidecar
        duplicate["images"].append(duplicate["images"][0])
        with self.assertRaisesRegex(verifier.Invalid, "duplicate source"):
            self.check(fixture, duplicate)
        for source in ("https://user:password@registry.example.org/a@sha256:" + "a" * 64,
                       "/private/example@sha256:" + "a" * 64, "example/image:tag"):
            sidecar = fixture.sidecar
            sidecar["images"][0]["source"] = source
            with self.assertRaisesRegex(verifier.Invalid, "credential-free"):
                self.check(fixture, sidecar)

    def test_source_aliases_may_map_to_the_same_verified_config(self):
        fixture = Fixture()
        sidecar = fixture.sidecar
        sidecar["images"].append({"source": "mirror.example.org/copy@sha256:" + "a" * 64,
                                  "imageId": fixture.records[0]["imageId"]})
        result = self.check(fixture, sidecar)
        self.assertEqual((result["dockerImages"], result["sourceReferences"]), (1, 2))

    def test_missing_extra_or_wrong_config_image_mapping_is_rejected(self):
        fixture = Fixture(2)
        for records in (fixture.records[:1], fixture.records + [{"source": "example/extra@sha256:" + "c" * 64,
                                                                "imageId": "sha256:" + "d" * 64}],
                        [{"source": item["source"], "imageId": "sha256:" + "e" * 64} for item in fixture.records]):
            with self.subTest(records=records), self.assertRaisesRegex(verifier.Invalid, "image set differs"):
                self.check(fixture, {"images": records})

    def test_missing_second_docker_image_cannot_be_hidden_by_partial_oci_index(self):
        fixture = Fixture(2).oci()
        fixture.oci(fixture.descriptors[:1])
        fixture.files["manifest.json"] = encode(fixture.entries[:1])
        with self.assertRaisesRegex(verifier.Invalid, "image set differs"):
            self.check(fixture)

    def test_tampered_second_docker_image_cannot_be_hidden_by_partial_oci_index(self):
        fixture = Fixture(2).oci()
        fixture.oci(fixture.descriptors[:1])
        fixture.files[fixture.entries[1]["Layers"][0]] += b"tampered"
        with self.assertRaisesRegex(verifier.Invalid, "blob SHA256 differs"):
            self.check(fixture)

    def test_unreferenced_content_addressed_blob_is_also_hashed(self):
        fixture = Fixture()
        fixture.files["blobs/sha256/" + "0" * 64] = b"tampered orphan blob"
        with self.assertRaisesRegex(verifier.Invalid, "blob SHA256 differs"):
            self.check(fixture)

    def test_matching_blob_hash_does_not_replace_config_diffid_check(self):
        with self.assertRaisesRegex(verifier.Invalid, "DiffID differs"):
            self.check(Fixture(bad_diffid=True))

    def test_missing_layer_or_config_is_rejected(self):
        for field in ("Layers", "Config"):
            fixture = Fixture()
            name = fixture.entries[0][field]
            del fixture.files[name[0] if isinstance(name, list) else name]
            with self.subTest(field=field), self.assertRaisesRegex(verifier.Invalid, "missing"):
                self.check(fixture)

    def test_non_amd64_image_is_rejected_even_if_mapping_and_hashes_match(self):
        with self.assertRaisesRegex(verifier.Invalid, "linux/amd64"):
            self.check(Fixture(platform="arm64"))

    def test_duplicate_manifest_image_is_rejected(self):
        fixture = Fixture()
        fixture.files["manifest.json"] = encode(fixture.entries * 2)
        with self.assertRaisesRegex(verifier.Invalid, "duplicates a config"):
            self.check(fixture)

    def test_oci_descriptor_size_and_digest_are_checked(self):
        for field, value in (("size", 0), ("digest", "sha256:" + "e" * 64)):
            fixture = Fixture().oci()
            changed = copy.deepcopy(fixture.descriptors)
            changed[0][field] = value
            fixture.oci(changed)
            with self.subTest(field=field), self.assertRaises(verifier.Invalid):
                self.check(fixture)

    def test_extra_oci_image_not_listed_by_docker_is_rejected(self):
        fixture = Fixture(2).oci()
        fixture.files["manifest.json"] = encode(fixture.entries[:1])
        with self.assertRaisesRegex(verifier.Invalid, "unexpected image"):
            self.check(fixture, {"images": fixture.records[:1]})

    def test_oci_layers_must_match_the_config_even_when_all_blob_hashes_match(self):
        fixture = Fixture().oci()
        descriptor = fixture.descriptors[0]
        image = json.loads(fixture.files["blobs/sha256/" + descriptor["digest"].split(":")[1]])
        different_layer = layer_bytes("different-content")
        layer_digest = digest(different_layer)
        fixture.files["blobs/sha256/" + layer_digest] = different_layer
        image["layers"][0].update(digest="sha256:" + layer_digest, size=len(different_layer))
        encoded = encode(image)
        fixture.files["blobs/sha256/" + digest(encoded)] = encoded
        changed = dict(descriptor, digest="sha256:" + digest(encoded), size=len(encoded))
        fixture.oci([changed])
        with self.assertRaisesRegex(verifier.Invalid, "OCI layer uncompressed DiffID differs"):
            self.check(fixture)

    def test_oci_platform_cannot_disagree_with_the_verified_image(self):
        fixture = Fixture().oci()
        changed = copy.deepcopy(fixture.descriptors)
        changed[0]["platform"] = {"os": "linux", "architecture": "arm64"}
        fixture.oci(changed)
        with self.assertRaisesRegex(verifier.Invalid, "descriptor platform"):
            self.check(fixture)

    def test_safe_directories_are_allowed_but_unsafe_paths_are_rejected(self):
        directory = tarfile.TarInfo("safe-directory/")
        directory.type = tarfile.DIRTYPE
        self.assertEqual(self.check(Fixture(), extra=[(directory, None)])["dockerImages"], 1)
        for name in ("../escape", "/absolute", "a/../escape", "a\\escape", "./manifest.json", "bad\nname", "C:/escape"):
            member = tarfile.TarInfo(name)
            with self.subTest(name=name), self.assertRaisesRegex(verifier.Invalid, "path is unsafe"):
                self.check(Fixture(), extra=[(member, b"")])

    def test_duplicate_members_and_special_types_are_rejected(self):
        duplicate = tarfile.TarInfo("manifest.json")
        with self.assertRaisesRegex(verifier.Invalid, "duplicate member"):
            self.check(Fixture(), extra=[(duplicate, b"")])
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE):
            member = tarfile.TarInfo("special")
            member.type, member.linkname = kind, "target"
            with self.subTest(kind=kind), self.assertRaisesRegex(verifier.Invalid, "member type"):
                self.check(Fixture(), extra=[(member, None)])

    def test_sidecar_archive_checksum_and_size_are_checked(self):
        fixture = Fixture()
        fixture.write(self.archive)
        sidecar = fixture.sidecar
        sidecar.update(archiveBytes=self.archive.stat().st_size, archiveSha256=digest(self.archive.read_bytes()))
        self.assertEqual(verifier.verify_archive(self.archive, sidecar)["dockerImages"], 1)
        sidecar["archiveBytes"] += 1
        with self.assertRaisesRegex(verifier.Invalid, "byte size differs"):
            verifier.verify_archive(self.archive, sidecar)
        sidecar["archiveBytes"] -= 1
        sidecar["archiveSha256"] = "0" * 64
        with self.assertRaisesRegex(verifier.Invalid, "checksum differs"):
            verifier.verify_archive(self.archive, sidecar)

    def test_missing_sidecar_cli_fails_without_echoing_private_paths(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = verifier.main(["--archive", str(self.archive), "--expected-images", str(self.archive) + ".missing"])
        self.assertEqual(result, 1)
        self.assertNotIn(self.directory.name, stderr.getvalue())

    def test_successful_cli_emits_only_sanitized_report(self):
        fixture = Fixture().oci()
        fixture.write(self.archive)
        sidecar = Path(self.directory.name) / "expected-images.json"
        sidecar.write_bytes(encode(fixture.sidecar))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = verifier.main(["--archive", str(self.archive), "--expected-images", str(sidecar)])
        self.assertEqual(result, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["restoreMap"], fixture.records)
        self.assertNotIn(self.directory.name, stdout.getvalue())
        self.assertNotIn("must-not-appear-in-summary", stdout.getvalue())

    def test_truncated_archive_and_duplicate_json_keys_fail(self):
        fixture = Fixture()
        fixture.write(self.archive)
        with self.archive.open("r+b") as stream:
            stream.truncate(513)
        with self.assertRaisesRegex(verifier.Invalid, "truncated"):
            verifier.verify_archive(self.archive, fixture.sidecar)
        with self.assertRaisesRegex(verifier.Invalid, "duplicate object"):
            verifier.parse_json(b'{"images":[],"images":[]}')


if __name__ == "__main__":
    unittest.main()
