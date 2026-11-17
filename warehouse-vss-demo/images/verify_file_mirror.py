#!/usr/bin/env python3
"""Verify a native ``oc image mirror --dir`` store without importing images.

Only filesystem and JSON reads are performed. Image configuration is inspected
solely for its operating system and architecture; environment/history content
is never included in results or diagnostics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
REPOSITORY_PART = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*\Z")
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
PACKAGE_PIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}\Z")
MANIFEST_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
CONFIG_TYPES = {
    "application/vnd.oci.image.config.v1+json",
    "application/vnd.docker.container.image.v1+json",
}
MAX_JSON_BYTES = 16 * 1024 * 1024


class VerificationError(ValueError):
    """The local store or its inventory failed verification."""


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def repository_parts(value):
    require(isinstance(value, str) and bool(value), "Missing fileRepository")
    parts = value.split("/")
    require(all(REPOSITORY_PART.fullmatch(part) for part in parts),
            "Unsafe fileRepository; use lowercase registry repository components")
    return parts


def inventory_images(inventory):
    require(isinstance(inventory, dict) and type(inventory.get("format")) is int
            and inventory["format"] == 1 and inventory.get("platform") == "linux/amd64",
            "Inventory must use format 1 and platform linux/amd64")
    images = inventory.get("images")
    require(isinstance(images, list) and bool(images), "Inventory images must be a nonempty array")
    seen = set()
    for item in images:
        require(isinstance(item, dict), "Malformed inventory image")
        pin, digest = item.get("packagePin"), item.get("platformDigest")
        require(isinstance(pin, str) and PACKAGE_PIN.fullmatch(pin),
                "Each packagePin must be an immutable SHA256 image reference")
        require(pin not in seen, "Duplicate inventory packagePin")
        seen.add(pin)
        require(isinstance(digest, str) and DIGEST.fullmatch(digest),
                "Each platformDigest must be a SHA256 digest")
        repository_parts(item.get("fileRepository"))
    return images


def file_identity(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


class Store:
    def __init__(self, directory):
        directory = Path(directory)
        require(not directory.is_symlink(), "Mirror directory itself must not be a symlink")
        self.root = directory.resolve(strict=True)
        require(self.root.is_dir() and (self.root / "v2").is_dir(),
                "Expected a mirror directory containing v2")
        self.files, self.inodes = {}, {}
        self.bytes_hashed = 0
        self.tags = []

    def content_path(self, path):
        parts = path.relative_to(self.root).parts
        require(len(parts) >= 4 and parts[0] == "v2" and parts[-2] in {"blobs", "manifests"},
                "Unexpected regular file outside native registry content directories")
        repository_parts("/".join(parts[1:-2]))
        name = parts[-1]
        if parts[-2] == "blobs":
            require(DIGEST.fullmatch(name), "Blob filename is not a SHA256 digest")
        else:
            require(DIGEST.fullmatch(name) or TAG.fullmatch(name),
                    "Manifest filename is neither a SHA256 digest nor a valid tag")
        return parts[-2], name

    def inspect_link(self, path):
        kind, name = self.content_path(path)
        require(kind == "manifests" and TAG.fullmatch(name) and not DIGEST.fullmatch(name),
                "Only manifest tags may be symlinks")
        target = Path(os.readlink(path))
        lexical = Path(os.path.abspath(path.parent / target))
        require(lexical.is_relative_to(self.root), "Manifest tag symlink escapes mirror directory")
        resolved = path.resolve(strict=True)
        require(resolved.is_relative_to(self.root) and resolved.parent == path.parent
                and DIGEST.fullmatch(resolved.name) and not lexical.is_symlink()
                and stat.S_ISREG(resolved.lstat().st_mode),
                "Manifest tag must target a contained regular digest manifest")
        self.tags.append((path, resolved))

    def hash_file(self, path):
        before = path.lstat()
        require(stat.S_ISREG(before.st_mode), "Registry content is not a regular file")
        identity = file_identity(before)
        if identity in self.inodes:
            result = self.inodes[identity]
        else:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                require(stat.S_ISREG(opened.st_mode) and file_identity(opened) == identity,
                        "Registry content changed before hashing")
                digest = hashlib.sha256()
                for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                    digest.update(block)
                require(file_identity(os.fstat(stream.fileno())) == identity,
                        "Registry content changed during hashing")
            result = ("sha256:" + digest.hexdigest(), before.st_size, identity)
            self.inodes[identity] = result
            self.bytes_hashed += before.st_size
        require(file_identity(path.lstat()) == identity, "Registry content changed after hashing")
        self.files[path] = result
        return result

    def scan(self):
        pending = [self.root]
        while pending:
            directory = pending.pop()
            for path in sorted(directory.iterdir()):
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    self.inspect_link(path)
                elif stat.S_ISDIR(mode):
                    pending.append(path)
                elif stat.S_ISREG(mode):
                    kind, name = self.content_path(path)
                    digest, _, _ = self.hash_file(path)
                    if DIGEST.fullmatch(name):
                        require(digest == name, "Content-addressed registry file checksum mismatch")
                    else:
                        self.tags.append((path, path.parent / digest))
                else:
                    raise VerificationError("Special files are not permitted in a mirror store")
        for tag, target in self.tags:
            require(target in self.files, "Manifest tag has no verified digest manifest")
            if not tag.is_symlink():
                require(self.files[tag][:2] == self.files[target][:2],
                        "Manifest tag content differs from its digest manifest")
        return self

    def checked_json(self, path):
        require(path in self.files, "Required manifest or blob is missing from the mirror")
        expected, size, identity = self.files[path]
        require(size <= MAX_JSON_BYTES, "Registry JSON exceeds the metadata size limit")
        require(not path.is_symlink() and file_identity(path.lstat()) == identity,
                "Verified registry JSON changed")
        raw = path.read_bytes()
        require(file_identity(path.lstat()) == identity and len(raw) == size
                and "sha256:" + hashlib.sha256(raw).hexdigest() == expected,
                "Registry JSON changed after content verification")
        try:
            result = json.loads(raw)
        except (UnicodeError, ValueError) as error:
            raise VerificationError("Registry metadata is not valid JSON") from error
        require(isinstance(result, dict), "Registry metadata must be a JSON object")
        return result

    def descriptor(self, repository, descriptor):
        require(isinstance(descriptor, dict), "Malformed image content descriptor")
        digest, size = descriptor.get("digest"), descriptor.get("size")
        require(isinstance(digest, str) and DIGEST.fullmatch(digest)
                and type(size) is int and size >= 0, "Invalid image content digest or size")
        path = repository / "blobs" / digest
        require(path in self.files, "Referenced image blob is missing")
        require(self.files[path][0] == digest and self.files[path][1] == size,
                "Referenced image blob digest or size differs")
        return path

    def image(self, item):
        repository = self.root / "v2" / Path(*repository_parts(item["fileRepository"]))
        manifest = self.checked_json(repository / "manifests" / item["platformDigest"])
        require(type(manifest.get("schemaVersion")) is int and manifest["schemaVersion"] == 2
                and manifest.get("mediaType") in MANIFEST_TYPES and "manifests" not in manifest,
                "platformDigest must identify a schema-2 leaf image manifest, not an index")
        configuration = manifest.get("config")
        require(isinstance(configuration, dict) and configuration.get("mediaType") in CONFIG_TYPES,
                "Image manifest has no supported configuration descriptor")
        config = self.checked_json(self.descriptor(repository, configuration))
        require(config.get("os") == "linux" and config.get("architecture") == "amd64",
                "Image configuration is not linux/amd64")
        layers = manifest.get("layers")
        require(isinstance(layers, list), "Image manifest layers must be an array")
        for layer in layers:
            self.descriptor(repository, layer)


def verify_file_mirror(directory, inventory):
    """Validate every inventory row and every content-addressed store file."""
    images = inventory_images(inventory)
    store = Store(directory).scan()
    verified = set()
    for item in images:
        key = (item["fileRepository"], item["platformDigest"])
        if key not in verified:
            store.image(item)
            verified.add(key)
    return {
        "format": 1,
        "contentValidated": True,
        "platform": "linux/amd64",
        "imageCounts": {
            "packagePins": len(images),
            "uniquePlatformImages": len({item["platformDigest"] for item in images}),
            "repositoryManifests": len(verified),
        },
        "filesValidated": len(store.files),
        "bytesValidated": sum(value[1] for value in store.files.values()),
        "bytesHashed": store.bytes_hashed,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        require(args.inventory.stat().st_size <= MAX_JSON_BYTES, "Inventory exceeds the metadata size limit")
        inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
        summary = verify_file_mirror(args.directory, inventory)
    except (OSError, RuntimeError, ValueError) as error:
        # Do not dump manifest/config JSON, image environment, or image history.
        summary = {"contentValidated": False, "error": str(error)}
        print(json.dumps(summary, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
