"""Offline safety contracts for the two selected source-build recipes."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "build/scripts/build-nvstreamer-ui.py"
SPEC = importlib.util.spec_from_file_location("build_runtime_images", MODULE_PATH)
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)
PIN = "registry.example/runtime@sha256:" + "a" * 64


class RuntimeBuildTests(unittest.TestCase):
    def test_selected_source_identities_and_p4_only(self):
        nv = build.SPECS["nvstreamer"]
        self.assertEqual(nv["commit"], "ac7d872f1a5ba5e5641562c0aa9a4a37138da390")
        self.assertEqual(nv["tree"], "d6a103d986f10fc23bc1e73b0ea2523f5e45d7f8")
        self.assertEqual([x[:4] for x in nv["patches"]], ["0001", "0002", "0003", "0004"])
        for component in build.SPECS.values():
            for filename, digest in component["patches"].items():
                build.check_hash(build.PATCHES / component["patchDir"] / filename, digest)

    def test_tag_rejects_latest_and_missing_tag(self):
        for tag in ("runtime", "runtime:latest", "runtime:LaTeSt", "runtime:v1;echo", "runtime@sha256:abc"):
            with self.assertRaises(ValueError):
                build.check_tag(tag)
        self.assertEqual(build.check_tag("registry.example:5000/demo/ui:3.2.1-p2"),
                         "registry.example:5000/demo/ui:3.2.1-p2")

    def test_mappings_require_unique_digest(self):
        self.assertEqual(build.parse_mappings(["node:22=" + PIN]), {"node:22": PIN})
        for values in (["node:22=node:22"], ["x=" + PIN, "x=" + PIN], ["bad"]):
            with self.assertRaises(ValueError):
                build.parse_mappings(values)

    def test_from_pins_resolve_default_args_preserve_stages(self):
        original = "ARG VER=22\nFROM node:${VER} AS builder\nFROM builder AS runner-prod\nARG BUILD_TYPE=prod\nFROM runner-${BUILD_TYPE}\n"
        pinned, bases = build.pin_dockerfile(original, {"node:22": PIN})
        self.assertEqual(bases, ["node:22"])
        self.assertIn("FROM " + PIN + " AS builder\n", pinned)
        self.assertIn("FROM runner-${BUILD_TYPE}\n", pinned)

    def test_missing_or_unused_base_mapping_fails(self):
        with self.assertRaisesRegex(ValueError, "missing immutable"):
            build.pin_dockerfile("FROM node:22\n", {})
        with self.assertRaisesRegex(ValueError, "unused"):
            build.pin_dockerfile("FROM " + PIN + "\n", {"other:1": PIN})

    def test_unknown_from_arg_fails(self):
        with self.assertRaisesRegex(ValueError, "unresolved"):
            build.pin_dockerfile("FROM ${MISSING}\n", {})

    def test_nested_cuda_defaults_resolve(self):
        original = "ARG CUDA_VERSION=13.2.0\nARG UBUNTU_VERSION=ubuntu24.04\nARG TOOLCHAIN_IMAGE=nvcr.io/nvidia/cuda:${CUDA_VERSION}-devel-${UBUNTU_VERSION}\nFROM ${TOOLCHAIN_IMAGE} AS builder\nFROM nvcr.io/nvidia/cuda:${CUDA_VERSION}-runtime-${UBUNTU_VERSION} AS runtime-base\n"
        pinned, bases = build.pin_dockerfile(original, {
            "nvcr.io/nvidia/cuda:13.2.0-devel-ubuntu24.04": PIN,
            "nvcr.io/nvidia/cuda:13.2.0-runtime-ubuntu24.04": PIN})
        self.assertEqual(len(bases), 2)
        self.assertEqual(pinned.count("FROM " + PIN), 2)

    def test_commands_never_publish_login_or_change_cluster(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nv = build.commands("nvstreamer", root, "warehouse/nvstreamer:p4")
            self.assertEqual(len(nv), 2)
            self.assertIn("runtime-amd64", nv[0])
            self.assertIn("MODULE=nvstreamer", nv[0])
            self.assertIn("BASE_IMAGE=warehouse/nvstreamer:p4-source", nv[1])
            for command in nv + build.commands("ui", root, "warehouse/ui:3.2.1-p2"):
                self.assertEqual(command[:3], ["docker", "buildx", "build"])
                self.assertIn("linux/amd64", command)
                self.assertIn("--load", command)
                self.assertNotIn("--push", command)
                self.assertNotIn("login", command)
                self.assertNotIn("oc", command)

    def test_wrong_commit_or_dirty_source_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory).resolve()
            with mock.patch.object(build, "run", side_effect=[str(source), "bad"]):
                with self.assertRaisesRegex(ValueError, "pinned commit"):
                    build.validate(source, "ui")
            with mock.patch.object(build, "run", side_effect=[str(source), build.SPECS["ui"]["commit"], " M changed"]):
                with self.assertRaisesRegex(ValueError, "dirty"):
                    build.validate(source, "ui")

    def test_plan_does_not_stage_or_build(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            dockerfile = source / build.SPECS["ui"]["dockerfile"]
            dockerfile.parent.mkdir(parents=True)
            dockerfile.write_text("FROM node:22\n")
            with mock.patch.object(build, "validate", return_value=build.SPECS["ui"]), \
                    mock.patch.object(build, "stage_source") as stage, \
                    mock.patch.object(build.subprocess, "run") as subprocess_run:
                self.assertEqual(build.main(["--component", "ui", "--source", str(source),
                                             "--tag", "warehouse/ui:3.2.1-p2", "--base-image", "node:22=" + PIN]), 0)
                stage.assert_not_called()
                subprocess_run.assert_not_called()

    def test_archive_rejects_traversal(self):
        import io
        import tarfile
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as archive:
            member = tarfile.TarInfo("../../escape")
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(build, "run", return_value=data.getvalue()):
                with self.assertRaisesRegex(ValueError, "escaping"):
                    build.stage_source(Path(directory), Path(directory), build.SPECS["ui"])

    def test_archive_rejects_escaping_symlink(self):
        import io
        import tarfile
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as archive:
            member = tarfile.TarInfo("services/vios/link")
            member.type = tarfile.SYMTYPE
            member.linkname = "../../../outside"
            archive.addfile(member)
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(build, "run", return_value=data.getvalue()):
                with self.assertRaisesRegex(ValueError, "escaping symlink"):
                    build.stage_source(Path(directory), Path(directory), build.SPECS["nvstreamer"])

    def test_lfs_materialized_object_verified_without_source_mutation(self):
        import hashlib
        import io
        import tarfile
        payload = b"verified binary payload"
        pointer = ("version https://git-lfs.github.com/spec/v1\noid sha256:"
                   + hashlib.sha256(payload).hexdigest() + "\nsize " + str(len(payload)) + "\n").encode()
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as archive:
            member = tarfile.TarInfo("services/vios/library.bin")
            member.size = len(pointer)
            archive.addfile(member, io.BytesIO(pointer))
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "source", Path(directory) / "stage"
            library = source / "services/vios/library.bin"
            library.parent.mkdir(parents=True)
            library.write_bytes(payload)
            target.mkdir()
            spec = {"subtree": "services/vios", "patchDir": "fixture", "patches": {}}
            with mock.patch.object(build, "run", side_effect=[data.getvalue(), "", ""]):
                build.stage_source(source, target, spec)
            self.assertEqual((target / "services/vios/library.bin").read_bytes(), payload)
            self.assertEqual(library.read_bytes(), payload)
            library.write_bytes(b"mismatched data")
            other = Path(directory) / "other"
            other.mkdir()
            with mock.patch.object(build, "run", return_value=data.getvalue()):
                with self.assertRaisesRegex(ValueError, "Git LFS object missing or mismatched"):
                    build.stage_source(source, other, spec)


if __name__ == "__main__":
    unittest.main()
