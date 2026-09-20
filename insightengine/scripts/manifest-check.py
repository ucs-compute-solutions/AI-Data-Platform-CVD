#!/usr/bin/env python3
"""Validate selected fields in a rendered InsightEngine YAML manifest."""

from __future__ import annotations

import argparse
import ast
import sys


def strip_comment(value: str) -> str:
    quote = ""
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote == '"':
            escaped = True
            continue
        if quote:
            if character == quote:
                quote = ""
            continue
        if character in ("'", '"'):
            quote = character
        elif character == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.rstrip()


def scalar(value: str) -> str:
    value = strip_comment(value.strip())
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            parsed = value[1:-1]
        return str(parsed)
    return value


def documents(lines: list[str]) -> list[dict[tuple[str, ...], str]]:
    parsed_documents: list[dict[tuple[str, ...], str]] = []
    current: dict[tuple[str, ...], str] = {}
    stack: list[tuple[int, tuple[str, ...]]] = [(-1, ())]
    for raw_line in lines + ["---"]:
        stripped = raw_line.strip()
        if stripped == "---":
            if current:
                parsed_documents.append(current)
            current = {}
            stack = [(-1, ())]
            continue
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            continue
        indentation = len(raw_line) - len(raw_line.lstrip(" "))
        key, separator, raw_value = raw_line.lstrip(" ").partition(":")
        if not separator or not key or any(character.isspace() for character in key):
            continue
        while stack[-1][0] >= indentation:
            stack.pop()
        path = stack[-1][1] + (key,)
        if raw_value.strip():
            current[path] = scalar(raw_value)
        else:
            stack.append((indentation, path))
    return parsed_documents


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--broker-name", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--image-repository", required=True)
    parser.add_argument("--image-tag", required=True)
    args = parser.parse_args()

    matches = [
        document
        for document in documents(sys.stdin.read().splitlines())
        if document.get(("kind",)) == "IngestionPipeline"
        and document.get(("metadata", "name")) == args.name
    ]

    if len(matches) != 1:
        raise SystemExit(1)
    document = matches[0]
    actual = {
        "topic": document.get(("spec", "topic", "name")),
        "broker_name": document.get(("spec", "topic", "broker")),
        "registry": document.get(("spec", "containerRegistryName")),
        "cluster": document.get(("spec", "kubernetesClusterName")),
        "image_repository": document.get(("spec", "image", "repository")),
        "image_tag": document.get(("spec", "image", "tag")),
    }
    expected = {
        "topic": args.topic,
        "broker_name": args.broker_name,
        "registry": args.registry,
        "cluster": args.cluster,
        "image_repository": args.image_repository,
        "image_tag": args.image_tag,
    }
    if actual != expected:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
