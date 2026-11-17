#!/usr/bin/env python3
"""Verify Docker-save contents against an independent source/config-ID sidecar.

Read-only and standard-library-only. No files are extracted and no images are
loaded, started, pulled or pushed. Content verification is not an engine-load
or application-runtime acceptance test.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import zlib

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
SOURCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}")
BLOB = re.compile(r"blobs/sha256/([0-9a-f]{64})")
CONFIG_NAME = re.compile(r"([0-9a-f]{64})\.json")
JSON_LIMIT = 8 * 1024 * 1024
INDEX_TYPES = {"application/vnd.oci.image.index.v1+json",
               "application/vnd.docker.distribution.manifest.list.v2+json"}
MANIFEST_TYPES = {"application/vnd.oci.image.manifest.v1+json",
                  "application/vnd.docker.distribution.manifest.v2+json"}


class Invalid(ValueError):
    """A sanitized archive-verification failure."""


def require(condition, message):
    if not condition:
        raise Invalid(message)


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "JSON contains duplicate object keys")
        value[key] = item
    return value


def parse_json(raw):
    require(len(raw) <= JSON_LIMIT, "JSON metadata exceeds the size limit")
    try:
        return json.loads(raw, object_pairs_hook=unique_object)
    except (UnicodeError, json.JSONDecodeError):
        raise Invalid("JSON metadata is malformed") from None


def sha256(stream):
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def safe_name(name):
    require(isinstance(name, str) and name and "\\" not in name and not re.match(r"^[A-Za-z]:", name)
            and not any(ord(char) < 32 or ord(char) == 127 for char in name),
            "Archive member path is unsafe")
    path = PurePosixPath(name)
    require(not path.is_absolute() and ".." not in path.parts
            and path.as_posix() not in (".", "")
            and path.as_posix() == name.rstrip("/"), "Archive member path is unsafe or ambiguous")
    return path.as_posix()


def expected_images(sidecar):
    require(isinstance(sidecar, dict) and isinstance(sidecar.get("images"), list)
            and 0 < len(sidecar["images"]) <= 4096,
            "Independent sidecar must contain nonempty images records")
    mapping = {}
    for record in sidecar["images"]:
        require(isinstance(record, dict), "Sidecar image record is malformed")
        source, ident = record.get("source"), record.get("imageId")
        require(isinstance(source, str) and len(source) <= 1024 and SOURCE.fullmatch(source)
                and "://" not in source and "//" not in source
                and ".." not in source.split("@", 1)[0].split("/"),
                "Sidecar source must be credential-free and digest-pinned")
        require(isinstance(ident, str) and DIGEST.fullmatch(ident), "Sidecar config image ID is malformed")
        require(source not in mapping, "Sidecar contains a duplicate source reference")
        mapping[source] = ident
    return mapping


class Archive:
    def __init__(self, archive):
        self.archive, self.members, self.hashes, self.layers = archive, {}, {}, {}
        self.oci_configs = set()
        for member in archive:
            require(len(self.members) < 100_000, "Archive contains too many members")
            name = safe_name(member.name)
            require(name not in self.members, "Archive contains duplicate member paths")
            require(member.type in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE)
                    and member.sparse is None and not any(key.startswith("GNU.sparse") for key in member.pax_headers),
                    "Archive contains an unsupported member type")
            require(member.isdir() or not member.name.endswith("/"), "Archive member path is unsafe or ambiguous")
            require(member.size >= 0 and (not member.isdir() or member.size == 0), "Archive member size is invalid")
            self.members[name] = member
        self.blobs = 0
        for name, member in self.members.items():
            if not member.isfile():
                continue
            match = BLOB.fullmatch(name)
            if name.startswith("blobs/"):
                require(match is not None, "Unsupported content-addressed blob path")
            if match:
                require(self.digest(name) == match[1], "Content-addressed blob SHA256 differs")
                self.blobs += 1
            legacy_config = CONFIG_NAME.fullmatch(name)
            if legacy_config:
                require(self.digest(name) == legacy_config[1], "Config filename SHA256 differs")

    def member(self, name):
        name = safe_name(name)
        require(name in self.members and self.members[name].isfile(), "Required archive file is missing")
        return self.members[name]

    def digest(self, name):
        member = self.member(name)
        if member.name not in self.hashes:
            with self.archive.extractfile(member) as stream:
                self.hashes[member.name] = sha256(stream)
        return self.hashes[member.name]

    def json(self, name):
        member = self.member(name)
        require(member.size <= JSON_LIMIT, "JSON metadata exceeds the size limit")
        with self.archive.extractfile(member) as stream:
            return parse_json(stream.read(JSON_LIMIT + 1))

    def config(self, name):
        ident = "sha256:" + self.digest(name)
        value = self.json(name)
        require(isinstance(value, dict) and value.get("os") == "linux"
                and value.get("architecture") == "amd64", "Image platform must be linux/amd64")
        rootfs = value.get("rootfs")
        require(isinstance(rootfs, dict) and rootfs.get("type") == "layers"
                and isinstance(rootfs.get("diff_ids"), list)
                and all(isinstance(item, str) and DIGEST.fullmatch(item) for item in rootfs["diff_ids"]),
                "Image RootFS DiffIDs are malformed")
        return ident, rootfs["diff_ids"]

    def layer(self, name):
        member = self.member(name)
        if member.name not in self.layers:
            with self.archive.extractfile(member) as stream:
                compressed = stream.read(2) == b"\x1f\x8b"
            if compressed:
                try:
                    with self.archive.extractfile(member) as stream, gzip.GzipFile(fileobj=stream) as decoded:
                        digest = sha256(decoded)
                except (OSError, EOFError, zlib.error):
                    raise Invalid("Compressed image layer is malformed") from None
            else:
                digest = self.digest(name)
            self.layers[member.name] = "sha256:" + digest
        return self.layers[member.name]

    def docker(self, expected):
        entries = self.json("manifest.json")
        require(isinstance(entries, list) and entries, "Docker manifest must contain image entries")
        seen, references = set(), 0
        for entry in entries:
            require(isinstance(entry, dict) and isinstance(entry.get("Config"), str)
                    and isinstance(entry.get("Layers"), list)
                    and all(isinstance(name, str) for name in entry["Layers"]), "Docker manifest entry is malformed")
            ident, diffids = self.config(entry["Config"])
            require(ident not in seen, "Docker manifest duplicates a config image ID")
            seen.add(ident)
            require(len(diffids) == len(entry["Layers"]), "Docker layer count differs from config RootFS")
            for name, expected_diffid in zip(entry["Layers"], diffids):
                require(self.layer(name) == expected_diffid, "Docker layer uncompressed DiffID differs")
                references += 1
        require(seen == expected, "Docker image set differs from independent sidecar")
        return len(seen), references

    def descriptor(self, value):
        require(isinstance(value, dict) and isinstance(value.get("digest"), str)
                and DIGEST.fullmatch(value["digest"]) and type(value.get("size")) is int
                and value["size"] >= 0, "OCI descriptor is malformed or unsupported")
        name = "blobs/sha256/" + value["digest"].split(":", 1)[1]
        require(self.member(name).size == value["size"] and "sha256:" + self.digest(name) == value["digest"],
                "OCI descriptor size or digest differs")
        return name

    def walk_oci(self, descriptor, expected, depth=0):
        require(depth <= 16, "OCI descriptor nesting exceeds the limit")
        name = self.descriptor(descriptor)
        media = descriptor.get("mediaType")
        value = self.json(name)
        require(isinstance(value, dict) and type(value.get("schemaVersion")) is int
                and value["schemaVersion"] == 2, "OCI manifest or index is malformed")
        if media in INDEX_TYPES:
            require(isinstance(value.get("manifests"), list), "OCI index manifests are malformed")
            for child in value["manifests"]:
                self.walk_oci(child, expected, depth + 1)
        else:
            require(media in MANIFEST_TYPES and isinstance(value.get("layers"), list), "OCI image manifest is unsupported")
            if "platform" in descriptor:
                platform = descriptor["platform"]
                require(isinstance(platform, dict) and platform.get("os") == "linux"
                        and platform.get("architecture") == "amd64", "OCI descriptor platform must be linux/amd64")
            config = self.descriptor(value.get("config"))
            ident, diffids = self.config(config)
            require(ident in expected, "OCI index contains an unexpected image")
            self.oci_configs.add(ident)
            require(len(value["layers"]) == len(diffids), "OCI layer count differs from config RootFS")
            for child, diffid in zip(value["layers"], diffids):
                require(self.layer(self.descriptor(child)) == diffid, "OCI layer uncompressed DiffID differs")

    def oci(self, expected):
        present = {name for name in ("oci-layout", "index.json") if name in self.members}
        if not present:
            return
        require(len(present) == 2 and self.json("oci-layout") == {"imageLayoutVersion": "1.0.0"}, "OCI layout is incomplete or unsupported")
        index = self.json("index.json")
        require(isinstance(index, dict) and type(index.get("schemaVersion")) is int and index["schemaVersion"] == 2
                and isinstance(index.get("manifests"), list), "OCI root index is malformed")
        for descriptor in index["manifests"]:
            self.walk_oci(descriptor, expected)


def verify_archive(path, sidecar):
    mapping = expected_images(sidecar)
    with Path(path).open("rb") as stream:
        before = os.fstat(stream.fileno())
        require(before.st_size >= 1024 and before.st_size % 512 == 0, "Archive is truncated or not an uncompressed tar")
        stream.seek(-1024, 2)
        require(stream.read(1024) == b"\0" * 1024, "Archive trailer is incomplete")
        stream.seek(0)
        archive_digest = sha256(stream)
        if "archiveSha256" in sidecar:
            require(sidecar["archiveSha256"] == archive_digest, "Archive checksum differs from independent sidecar")
        if "archiveBytes" in sidecar:
            require(type(sidecar["archiveBytes"]) is int and sidecar["archiveBytes"] == before.st_size,
                    "Archive byte size differs from independent sidecar")
        stream.seek(0)
        with tarfile.open(fileobj=stream, mode="r:", ignore_zeros=True) as tar:
            archive = Archive(tar)
            count, references = archive.docker(set(mapping.values()))
            archive.oci(set(mapping.values()))
        after = os.fstat(stream.fileno())
        require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns), "Archive changed during verification")
    return {"format": "docker-save-verification-v1", "status": "verified",
            "archiveBytes": before.st_size, "archiveSha256": archive_digest,
            "platform": "linux/amd64", "dockerImages": count, "sourceReferences": len(mapping),
            "contentAddressedBlobs": archive.blobs, "dockerLayerReferences": references,
            "ociIndexedImages": len(archive.oci_configs), "engineLoadTested": False,
            "restoreMap": [{"source": source, "imageId": mapping[source]} for source in sorted(mapping)]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--expected-images", type=Path, required=True,
                        help="Independent JSON with images records containing source and imageId")
    args = parser.parse_args(argv)
    try:
        with args.expected_images.open("rb") as stream:
            sidecar = parse_json(stream.read(JSON_LIMIT + 1))
        print(json.dumps(verify_archive(args.archive, sidecar), indent=2, sort_keys=True))
        return 0
    except Invalid as error:
        print("FAIL: " + str(error), file=sys.stderr)
    except (OSError, EOFError, tarfile.TarError, ValueError, KeyError, TypeError):
        print("FAIL: Archive or sidecar could not be read or decoded; details suppressed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
