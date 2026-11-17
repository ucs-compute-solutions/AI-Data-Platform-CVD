#!/usr/bin/env python3
"""Review and install a site-bound Warehouse candidate from a reviewed baseline.

This tool never reads Secret values, deletes data, upgrades an existing install,
or treats a historical migration plan as a new installation specification.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import ipaddress
import json
import re
import subprocess
import sys
from pathlib import Path

FORMAT = 1
CORRECTIONS = (
    "vios-shared-listener-v2", "vios-remote-postgresql", "vios-uid1000-runtime",
    "rtvi-cv-driver-libraries-launcher", "rtsp-tcp-four-camera", "nvstreamer-derived-p4",
    "rtvi-vlm-release-mode", "stateful-kafka-probes-dns", "ui-sensor-id",
    "ui-report-conversation-isolation", "agent-report-source-binding",
    "va-mcp-vlm-incident-lookup", "agent-incident-followup", "complete-network-flows",
)
GPU_ROLES = ("nvstreamer", "viosStreamprocessing", "rtviCv", "rtviVlm", "nemotron")
REQUIRED_ROLES = GPU_ROLES + ("viosSensor", "alertBridge", "vaMcp", "agent", "ui", "kafka", "postgres", "elasticsearch")
NAMESPACED_KINDS = {
    "ConfigMap", "PersistentVolumeClaim", "Service", "ServiceAccount", "Role", "RoleBinding",
    "NetworkPolicy", "Deployment", "StatefulSet", "DaemonSet", "Route", "PodDisruptionBudget", "Job",
}
CLUSTER_KINDS = {"SecurityContextConstraints", "ClusterRole"}
CONTROLLERS = {"Deployment", "StatefulSet", "DaemonSet", "Job"}
FOUNDATION = {"StatefulSet/warehouse-kafka", "StatefulSet/warehouse-elasticsearch", "StatefulSet/warehouse-redis", "StatefulSet/warehouse-vios-postgres", "Deployment/warehouse-kibana"}
IMMUTABLE_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*(?::[A-Za-z0-9_.-]+)?@sha256:[a-f0-9]{64}$")
DNS_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
TOKEN = re.compile(r"@@([A-Z][A-Z0-9_]*)@@")
SENSITIVE = re.compile(r"(?:password|passwd|credential|api[_-]?key|(?:^|[_-])(?:pwd|token)|access[_-]?token|auth[_-]?token|client[_-]?secret|secret)$", re.I)
CREDENTIAL_ASSIGNMENT = re.compile(r'''(?<![A-Za-z0-9_$])(?P<quote>["']?)(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)(?P=quote)\s*(?P<operator>[:=])\s*''')
SQL_PASSWORD = re.compile(r"(?i)(?<![A-Za-z0-9_$])PASSWORD\b(?=[\s=])\s*(?:=\s*)?")
DOLLAR_QUOTE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
PSQL_REFERENCE = re.compile(r''':(?:[A-Za-z_][A-Za-z0-9_]*|'[A-Za-z_][A-Za-z0-9_]*'|"[A-Za-z_][A-Za-z0-9_]*")''')
ENV_REFERENCE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*(?::?\?[^{}]*)?\})")
URL_USERINFO = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]*:([^/\s@]*)@")


class Invalid(ValueError):
    pass


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def read_json(path: Path):
    def invalid_constant(value):
        raise Invalid(f"Non-finite JSON number: {value}")
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=invalid_constant)


def write_json(path: Path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def expect(condition, message):
    if not condition:
        raise Invalid(message)


def walk(value, path=()):
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk(child, path + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, path + (str(index),))


def reference_key(resource):
    metadata = resource.get("metadata", {})
    return f"{resource.get('kind', '')}/{metadata.get('name', '')}"


def _runtime_credential_reference(value):
    return bool(ENV_REFERENCE.fullmatch(value) or PSQL_REFERENCE.fullmatch(value) or TOKEN.fullmatch(value))


def _without_credential_comments(text):
    """Remove comments outside strings; preserve multiline quoted values."""
    result, index, quote, dollar = [], 0, None, None
    while index < len(text):
        if dollar:
            if text.startswith(dollar, index):
                result.append(dollar)
                index += len(dollar)
                dollar = None
                continue
        elif quote:
            if text[index] == "\\" and index + 1 < len(text):
                result.append(text[index:index + 2])
                index += 2
                continue
            if text[index] == quote:
                quote = None
        else:
            boundary = index == 0 or text[index - 1].isspace() or text[index - 1] == ";"
            line_start = (boundary and (text.startswith("--", index) or text[index] == ";")
                          and not text[text.rfind("\n", 0, index) + 1:index].strip())
            if boundary and text.startswith("/*", index):
                end = text.find("*/", index + 2)
                end = len(text) if end < 0 else end + 2
                result.append("\n" * text[index:end].count("\n") or " ")
                index = end
                continue
            if boundary and (text.startswith(("#", "//"), index)
                             or line_start and text.startswith("--", index)
                             or line_start and text[index] == ";"
                             and not re.match(r"(?i);\s*(?:ALTER|CREATE|SET|SELECT|DO)\b", text[index:])):
                end = text.find("\n", index)
                index = len(text) if end < 0 else end
                continue
            if text[index] in "\"'`":
                quote = text[index]
            else:
                match = DOLLAR_QUOTE.match(text, index)
                if match:
                    dollar = match.group()
                    result.append(dollar)
                    index = match.end()
                    continue
        result.append(text[index])
        index += 1
    return "".join(result)


def _credential_token(text, start, *, shell=False):
    """Return a complete quoted, dollar-quoted or shell-expression value."""
    if start >= len(text):
        return "", "bare", start
    endings = ";&|" if shell else ",;})]&?"
    psql = PSQL_REFERENCE.match(text, start)
    if psql:
        if psql.end() < len(text) and not text[psql.end()].isspace() and text[psql.end()] not in endings:
            return text[start:psql.end() + 1], "concatenated", psql.end() + 1
        return psql.group(), "reference", psql.end()
    dollar = None if shell else DOLLAR_QUOTE.match(text, start)
    if dollar:
        end = text.find(dollar.group(), dollar.end())
        if end < 0:
            return "", "invalid", len(text)
        return text[dollar.end():end], "dollar", end + len(dollar.group())
    position = start
    if not shell and text[position] in "eEnN" and position + 1 < len(text) and text[position + 1] in "\"'":
        position += 1
    if text[position] in "\"'":
        quote, begin = text[position], position + 1
        position = begin
        while position < len(text):
            if text[position] == "\\":
                position += 2
                continue
            if text[position] == quote:
                end = position + 1
                if end < len(text) and not text[end].isspace() and text[end] not in endings:
                    return text[start:end + 1], "concatenated", end + 1
                return text[begin:position], "quoted", end
            position += 1
        return "", "invalid", len(text)
    stack = []
    while position < len(text):
        character = text[position]
        if text.startswith(("${", "$("), position):
            stack.append("}" if text[position + 1] == "{" else ")")
            position += 2
            continue
        if stack and character == stack[-1]:
            stack.pop()
        elif not stack and (character.isspace() or character in endings):
            break
        position += 1
    return text[start:position], "bare", position


def check_credential_text(text, location):
    """Reject likely literal credentials without echoing any matched values.

    Exact environment/psql/template references are data bindings, not blanket
    exemptions for defaults, concatenated strings or command substitutions.
    """
    message = f"Possible literal credential at {location}; matched values suppressed; reconstruct Secret injection"

    def safe(value, kind="quoted", *, configuration=False):
        return (kind not in {"invalid", "concatenated"}
                and (value == "" or kind != "dollar" and _runtime_credential_reference(value)
                     or configuration and kind == "bare" and value in {"null", "None", "false", "true", "~"}))

    def inspect(value, depth=0):
        expect(depth <= 24, "Credential inspection nesting exceeds its bounded limit")
        if isinstance(value, dict):
            for key, child in value.items():
                if SENSITIVE.search(str(key)) and not isinstance(child, (dict, list)):
                    expect(child is None or type(child) is bool or isinstance(child, str) and safe(child), message)
                inspect(child, depth + 1)
            return
        if isinstance(value, list):
            for child in value:
                inspect(child, depth + 1)
            return
        if not isinstance(value, str):
            return
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            pass
        else:
            if isinstance(decoded, (dict, list, str)) and decoded != value:
                inspect(decoded, depth + 1)
                return
        cleaned = _without_credential_comments(re.sub(r"\\\r?\n[ \t]*", "", value))
        sql_references = []
        for match in SQL_PASSWORD.finditer(cleaned):
            start = match.end()
            reference = PSQL_REFERENCE.match(cleaned, start)
            if reference:
                token, kind, end = _credential_token(cleaned, start)
                expect(safe(token, kind), message)
                sql_references.append((match.start(), reference.end()))
                continue
            prefix = cleaned[start:start + 2]
            if prefix[:1] in {"'", '"'} or prefix in {"E'", 'e\'', "N'", 'n\'', 'E"', 'e"', 'N"', 'n"'} or DOLLAR_QUOTE.match(cleaned, start):
                token, kind, _ = _credential_token(cleaned, start)
                expect(safe(token, kind), message)
        for match in CREDENTIAL_ASSIGNMENT.finditer(cleaned):
            if not SENSITIVE.search(match["name"]) or any(start <= match.start() < end for start, end in sql_references):
                continue
            token, kind, _ = _credential_token(cleaned, match.end(), shell=match["operator"] == "=")
            expect(safe(token, kind, configuration=match["operator"] == ":"), message)
        for match in URL_USERINFO.finditer(cleaned):
            expect(safe(match.group(1)), message)
    inspect(text)


def check_no_credentials(resources):
    for resource in resources:
        expect(resource.get("kind") != "Secret", "Secret objects are never accepted; provision credentials separately")
        for path, value in walk(resource):
            if isinstance(value, str) and "binaryData" not in path:
                check_credential_text(value, reference_key(resource) + ":" + "/".join(path))
            if not isinstance(value, dict):
                continue
            if "name" in value and "value" in value and SENSITIVE.search(str(value["name"])):
                expect(value["value"] in (None, ""), f"Literal credential-like environment value at {reference_key(resource)}:{'/'.join(path)}")
            if path and path[-1] in {"data", "stringData", "binaryData"} and resource.get("kind") == "ConfigMap":
                if path[-1] == "binaryData":
                    for filename, encoded in value.items():
                        expect(filename in {"Top.png", "ext.desc", "schema.desc"}, "Unrecognized binary ConfigMap artifact requires separate source review")
                        raw = base64.b64decode(encoded, validate=True)
                        expect(bool(raw) and (filename.endswith(".desc") or raw.startswith(b"\x89PNG\r\n\x1a\n")), "Invalid reviewed calibration/protobuf artifact")
                    continue
                check_credential_text(json.dumps(value), reference_key(resource) + ":" + "/".join(path))


def validate_site(site):
    required = {"format", "namespace", "cluster", "ingress", "storage", "placement", "auth", "images", "security", "network", "media"}
    expect(isinstance(site, dict) and set(site) == required and site.get("format") == FORMAT, "Site input must match site.schema.json format 1")
    expect(isinstance(site["namespace"], str) and re.fullmatch(r"[a-z][a-z0-9-]{0,61}[a-z0-9]|[a-z]", site["namespace"]), "Invalid target namespace")
    expect(set(site["cluster"]) == {"server", "context", "user", "domain"}, "cluster requires server/context/user/domain")
    expect(re.fullmatch(r"https://[a-zA-Z0-9.-]+(?::[0-9]+)?", site["cluster"]["server"]) is not None, "Expected a verified HTTPS cluster API without credentials or path")
    for name in ("context", "user", "domain"):
        expect(isinstance(site["cluster"][name], str) and site["cluster"][name].strip() == site["cluster"][name] and site["cluster"][name], f"cluster.{name} is required")
    expect(DNS_NAME.fullmatch(site["cluster"]["domain"]) is not None, "Invalid cluster DNS domain")
    expect(set(site["ingress"]) == {"publicHost", "routeHosts"} and DNS_NAME.fullmatch(site["ingress"]["publicHost"]) is not None, "ingress requires publicHost and routeHosts")
    expect(isinstance(site["ingress"]["routeHosts"], dict) and all(isinstance(k, str) and isinstance(v, str) and DNS_NAME.fullmatch(v) for k, v in site["ingress"]["routeHosts"].items()), "Invalid route host map")
    expect(set(site["storage"]) == {"rwoClass", "rwxClass"} and all(isinstance(v, str) and v for v in site["storage"].values()), "Two reviewed storage classes are required")
    expect(set(site["placement"]) == {"gpuNodeSelector", "cpuNodeSelector"}, "placement requires GPU and CPU node selectors")
    for selector in site["placement"].values():
        expect(isinstance(selector, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in selector.items()), "Node selectors must be string maps")
    expect(bool(site["placement"]["gpuNodeSelector"]), "An explicit GPU worker selector is required")
    expect(set(site["auth"]) == {"imagePullSecrets", "secretNames"}, "auth accepts references only")
    expect(isinstance(site["auth"]["imagePullSecrets"], list) and site["auth"]["imagePullSecrets"] and all(isinstance(v, str) and v for v in site["auth"]["imagePullSecrets"]), "At least one existing image-pull Secret reference is required")
    expect(isinstance(site["auth"]["secretNames"], dict) and all(isinstance(k, str) and isinstance(v, str) and v for k, v in site["auth"]["secretNames"].items()), "auth.secretNames maps captured Secret names to existing target names, never values")
    expect(isinstance(site["images"], dict) and set(site["images"]) == {"repositoryMirrors", "reviewedImageOverrides"} and isinstance(site["images"]["repositoryMirrors"], dict), "images requires repositoryMirrors and reviewedImageOverrides maps")
    for old, new in site["images"]["repositoryMirrors"].items():
        expect(isinstance(old, str) and isinstance(new, str) and all(v and "://" not in v and IMMUTABLE_IMAGE.fullmatch(v + "@sha256:" + "0" * 64)
               and ":" not in v.rsplit("/", 1)[-1] and not v.endswith("/") for v in (old, new)),
               "Mirror mappings contain nonempty repositories, not credentials, tags or digests")
    expect(isinstance(site["images"]["reviewedImageOverrides"], dict), "reviewedImageOverrides must map complete source pins to reviewed rebuilt target pins")
    for old, new in site["images"]["reviewedImageOverrides"].items():
        expect(isinstance(old, str) and isinstance(new, str) and IMMUTABLE_IMAGE.fullmatch(old) and IMMUTABLE_IMAGE.fullmatch(new)
               and not re.search(r":latest(?:@|$)", old + " " + new, re.I), "Rebuilt image overrides require two full sha256 pins; unreviewed tags/latest are prohibited")
    expect(set(site["network"]) == {"addresses", "cidrs"} and all(isinstance(value, dict) for value in site["network"].values()), "network requires explicit address and CIDR token maps")
    for key, value in site["network"]["addresses"].items():
        expect(re.fullmatch(r"NET_ADDRESS_[0-9]+", key) and isinstance(value, str) and DNS_NAME.fullmatch(value), "Invalid network address token or hostname")
    for key, value in site["network"]["cidrs"].items():
        expect(re.fullmatch(r"NET_CIDR_[0-9]+", key) is not None, "Invalid network CIDR token")
        try: ipaddress.ip_network(value, strict=True)
        except ValueError: raise Invalid("Invalid target network CIDR")
    expect(set(site["security"]) == {"sccName", "clusterRoleName", "includeClusterResources"} and type(site["security"]["includeClusterResources"]) is bool, "security requires scoped SCC/role names and explicit cluster-resource choice")
    for name in ("sccName", "clusterRoleName"):
        expect(site["security"][name].startswith(site["namespace"] + "-"), "Cluster-scoped names must start with the target namespace prefix")
    expect(set(site["media"]) == {"sourceSubPath", "sourceSeconds"} and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", site["media"]["sourceSubPath"])
           and site["media"]["sourceSeconds"] in (600, 3600), "media requires a reviewed single-directory subpath and 600 or 3600 seconds")
    return site


def baseline_gaps(baseline):
    gaps = []
    if baseline.get("format") != FORMAT or baseline.get("kind") != "warehouse-runtime-template":
        gaps.append("A format-1 warehouse-runtime-template is required")
    if baseline.get("status") not in {"reviewed-accepted-runtime", "reviewed-runtime-candidate"}:
        gaps.append("Accepted-runtime reconstruction has not been reviewed")
    if baseline.get("secretReviewComplete") is not True:
        gaps.append("Credential review is incomplete")
    for ident in CORRECTIONS:
        entry = baseline.get("corrections", {}).get(ident, {})
        if entry.get("status") != "verified" or not re.fullmatch(r"[a-f0-9]{64}", str(entry.get("evidenceSha256", ""))):
            gaps.append(f"Missing accepted correction evidence: {ident}")
    for role in REQUIRED_ROLES:
        if not baseline.get("components", {}).get(role):
            gaps.append(f"Missing component identity: {role}")
    if not baseline.get("resources"):
        gaps.append("Accepted desired Kubernetes resources have not been reconstructed")
    if not baseline.get("bootstrap", {}).get("reviewed"):
        gaps.append("Fresh database/index/model/video initialization recipe is not reviewed")
    return gaps


def substitute(value, tokens):
    if isinstance(value, dict):
        return {key: substitute(child, tokens) for key, child in value.items()}
    if isinstance(value, list):
        return [substitute(child, tokens) for child in value]
    if isinstance(value, str):
        def replace(match):
            expect(match.group(1) in tokens, f"Undeclared site token: {match.group(1)}")
            return tokens[match.group(1)]
        return TOKEN.sub(replace, value)
    return value


def pod_spec(resource):
    return resource.get("spec", {}).get("template", {}).get("spec") if resource.get("kind") in CONTROLLERS else None


def rewrite_image(image, mirrors, reviewed_overrides=None):
    expect(isinstance(image, str) and IMMUTABLE_IMAGE.fullmatch(image), "Every container and init-container image requires an immutable sha256 digest")
    if reviewed_overrides and image in reviewed_overrides:
        target = reviewed_overrides[image]
        expect(isinstance(target, str) and IMMUTABLE_IMAGE.fullmatch(target) and not re.search(r":latest(?:@|$)", target, re.I), "Reviewed rebuilt target must have a complete immutable sha256 pin")
        return target
    repository_tag, digest_value = image.split("@", 1)
    last = repository_tag.rsplit("/", 1)[-1]
    repository = repository_tag.rsplit(":", 1)[0] if ":" in last else repository_tag
    result = mirrors.get(repository, repository_tag) + "@" + digest_value
    expect(IMMUTABLE_IMAGE.fullmatch(result) and not re.search(r":latest(?:@|$)", result, re.I), "Image mirror result must retain a valid immutable sha256 pin")
    return result


def compile_resources(baseline, site, *, preview=False, bootstrap=False):
    validate_site(site)
    gaps = baseline_gaps(baseline)
    expect(preview or not gaps, "Baseline is incomplete: " + "; ".join(gaps))
    expect(not re.search(r"\.svc\.cluster\.local(?![A-Za-z0-9_.-])", canonical(baseline["resources"]).decode()),
           "Captured service DNS must use @@CLUSTER_DOMAIN@@, not a hard-coded cluster.local suffix")
    tokens = {"NAMESPACE": site["namespace"], "PUBLIC_HOST": site["ingress"]["publicHost"],
              "CLUSTER_DOMAIN": site["cluster"]["domain"], "SCC_NAME": site["security"]["sccName"],
              "CLUSTER_ROLE_NAME": site["security"]["clusterRoleName"]}
    tokens.update(site["network"]["addresses"])
    tokens.update(site["network"]["cidrs"])
    resources = substitute(copy.deepcopy(baseline["resources"] + (baseline.get("bootstrap", {}).get("jobs", []) if bootstrap else [])), tokens)
    expect(isinstance(resources, list), "resources must be a list")
    expect("[REDACTED]" not in canonical(resources).decode(), "Redacted runtime fields must be reconstructed and reviewed before rendering")
    check_no_credentials(resources)
    identities = set()
    for resource in resources:
        expect(isinstance(resource, dict) and resource.get("apiVersion"), "Invalid Kubernetes resource")
        kind, metadata = resource.get("kind"), resource.get("metadata", {})
        expect(kind in NAMESPACED_KINDS | CLUSTER_KINDS, f"Unsupported resource kind: {kind}; initialization Jobs require their own reviewed recipe")
        expect(metadata.get("name") and not set(metadata) & {"uid", "resourceVersion", "managedFields", "creationTimestamp", "ownerReferences"} and "status" not in resource,
               f"Remove captured server state from {reference_key(resource)}")
        key = reference_key(resource)
        expect(key not in identities, f"Duplicate resource: {key}")
        identities.add(key)
        if kind in CLUSTER_KINDS:
            expect(site["security"]["includeClusterResources"], "This baseline contains cluster-scoped prerequisites; explicitly review/enable them")
            expect(metadata["name"].startswith(site["namespace"] + "-") and not metadata.get("namespace"), "Cluster resource scope/name is not site-bound")
        else:
            expect(metadata.get("namespace") == site["namespace"], f"Foreign or missing namespace in {key}")
        if kind == "PersistentVolumeClaim":
            spec = resource["spec"]
            expect(not spec.get("volumeName") and not spec.get("selector"), "Fresh PVCs cannot bind a retained PV or selector")
            spec["storageClassName"] = site["storage"]["rwxClass" if "ReadWriteMany" in spec.get("accessModes", []) else "rwoClass"]
        if kind == "Service":
            expect(resource.get("spec", {}).get("clusterIP") in (None, "None") and not resource.get("spec", {}).get("clusterIPs"), "Remove assigned Service IPs; retain only headless clusterIP=None")
            expect(resource.get("spec", {}).get("type", "ClusterIP") == "ClusterIP", "Reviewed candidate exposes Services through Routes, not inherited NodePorts")
        if kind == "Route":
            expect(metadata["name"] in site["ingress"]["routeHosts"], f"Missing target hostname for Route/{metadata['name']}")
            resource["spec"]["host"] = site["ingress"]["routeHosts"][metadata["name"]]
        if kind == "ServiceAccount":
            resource["imagePullSecrets"] = [{"name": name} for name in site["auth"]["imagePullSecrets"]]
        spec = pod_spec(resource)
        if spec is not None:
            expect(not spec.get("nodeName"), f"Inherited nodeName in {key}; use reviewed target selectors")
            gpu = any(int(container.get("resources", {}).get("limits", {}).get("nvidia.com/gpu", 0)) for container in spec.get("containers", []))
            spec["nodeSelector"] = copy.deepcopy(site["placement"]["gpuNodeSelector" if gpu else "cpuNodeSelector"])
            spec["imagePullSecrets"] = [{"name": name} for name in site["auth"]["imagePullSecrets"]]
            for container in spec.get("containers", []) + spec.get("initContainers", []):
                container["image"] = rewrite_image(container.get("image"), site["images"]["repositoryMirrors"], site["images"]["reviewedImageOverrides"])
                expect("registry.example.com/" not in container["image"], "Map each preserved derived-image repository to a real verified registry")
            for claim in resource.get("spec", {}).get("volumeClaimTemplates", []):
                claim["spec"]["storageClassName"] = site["storage"]["rwxClass" if "ReadWriteMany" in claim["spec"].get("accessModes", []) else "rwoClass"]
        for path, value in walk(resource):
            if not isinstance(value, dict):
                continue
            for field in ("secretKeyRef", "secretRef", "secret"):
                ref = value.get(field)
                if isinstance(ref, dict):
                    name_key = "secretName" if field == "secret" else "name"
                    old = ref.get(name_key)
                    expect(old in site["auth"]["secretNames"], f"Unmapped Secret reference {old} in {key}")
                    ref[name_key] = site["auth"]["secretNames"][old]
            if kind == "ServiceAccount" and "secrets" in value:
                raise Invalid("Remove automatically generated ServiceAccount token references")
            for field in ("configMap", "configMapRef", "configMapKeyRef"):
                ref = value.get(field)
                if isinstance(ref, dict) and not ref.get("optional"):
                    expect(f"ConfigMap/{ref.get('name')}" in identities or any(reference_key(item) == f"ConfigMap/{ref.get('name')}" for item in resources), f"Unresolved ConfigMap reference in {key}: {ref.get('name')}")
    for role, key in baseline["components"].items():
        expect(key in identities, f"Missing desired resource for {role}: {key}")
    for role in GPU_ROLES:
        resource = next(item for item in resources if reference_key(item) == baseline["components"][role])
        spec = pod_spec(resource)
        count = sum(int(item.get("resources", {}).get("limits", {}).get("nvidia.com/gpu", 0)) for item in spec.get("containers", []))
        expect(count == 1, f"Expected one shared GPU allocation for {role}")
    source_key = baseline["components"]["nvstreamer"]
    source = next(item for item in resources if reference_key(item) == source_key)
    expect(source.get("spec", {}).get("replicas") == 0, "Install with NvStreamer stopped; data/model initialization and UI-rule creation precede source start")
    for container in pod_spec(source).get("containers", []):
        for mount in container.get("volumeMounts", []):
            if mount.get("mountPath") == "/tmp/nv_streamer/videos":
                mount["subPath"] = site["media"]["sourceSubPath"]
                expect(mount.get("readOnly") is True, "Source media mount must remain read-only")
    encoded = canonical(resources).decode()
    expect(not TOKEN.search(encoded), "Unresolved site token")
    for forbidden in baseline.get("forbiddenSourceStrings", []):
        expect(forbidden not in encoded, "A captured site address or placement remains unresolved")
    return sorted(resources, key=lambda r: (phase(r), reference_key(r)))


def phase(resource):
    return {"SecurityContextConstraints": 0, "ClusterRole": 0, "ServiceAccount": 1,
            "Role": 1, "RoleBinding": 1, "PersistentVolumeClaim": 2, "ConfigMap": 3,
            "Service": 3, "NetworkPolicy": 3, "StatefulSet": 4, "Deployment": 5,
            "DaemonSet": 5, "PodDisruptionBudget": 6, "Route": 6, "Job": 7}[resource["kind"]]


def staged(resources):
    resources = copy.deepcopy(resources)
    desired = {}
    for item in resources:
        if item["kind"] in {"Deployment", "StatefulSet"}:
            key = reference_key(item)
            desired[key] = item["spec"].get("replicas", 1)
            if key not in FOUNDATION: item["spec"]["replicas"] = 0
    return resources, desired


def render(baseline, site, output: Path):
    expect(not output.exists(), "Output already exists; retain reviewed plans and choose a new directory")
    resources, desired = staged(compile_resources(baseline, site))
    manifests = {"apiVersion": "v1", "kind": "List", "items": resources}
    jobs = {"apiVersion": "v1", "kind": "List", "items": [item for item in compile_resources(baseline, site, bootstrap=True) if item["kind"] == "Job"]}
    plan = {"format": FORMAT, "kind": "warehouse-fresh-install-plan", "baselineSha256": digest(baseline),
            "site": site, "siteSha256": digest(site), "manifestsSha256": digest(manifests),
            "resources": [reference_key(item) for item in resources], "operation": "create-only",
            "sourceStartsAutomatically": False, "freshInstallAccepted": False, "desiredReplicas": desired,
            "bootstrapSha256": digest(jobs), "sourceMedia": {"seconds": site["media"]["sourceSeconds"], "subPath": site["media"]["sourceSubPath"]}}
    plan["planSha256"] = digest(plan)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "manifests.json", manifests)
    write_json(output / "initialization-jobs.json", jobs)
    write_json(output / "plan.json", plan)
    return plan


def preview(baseline, site, output: Path):
    expect(not output.exists(), "Output already exists; select a new preview directory")
    resources, desired = staged(compile_resources(baseline, site, preview=True, bootstrap=True))
    manifests = {"apiVersion": "v1", "kind": "List", "items": resources}
    report = {"format": FORMAT, "kind": "warehouse-unaccepted-preview", "status": "not-installable",
              "baselineSha256": digest(baseline), "siteSha256": digest(site),
              "manifestsSha256": digest(manifests), "resources": len(resources), "gaps": baseline_gaps(baseline),
              "clusterAccess": False, "freshInstallAccepted": False}
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "manifests.preview.json", manifests)
    write_json(output / "preview-report.json", report)
    return report


def oc(arguments, *, data=None, timeout=300):
    completed = subprocess.run(["oc", *arguments], input=data, text=True, capture_output=True, check=False, timeout=timeout)
    expect(completed.returncode == 0, "oc command failed: " + " ".join(arguments[:4]) + "; inspect the client terminal without dumping credentials")
    return completed.stdout.strip()


def load_plan(directory: Path):
    plan = read_json(directory / "plan.json")
    manifests = read_json(directory / "manifests.json")
    content = {key: value for key, value in plan.items() if key != "planSha256"}
    expect(plan.get("kind") == "warehouse-fresh-install-plan" and plan.get("format") == FORMAT and plan.get("operation") == "create-only", "Unrecognized install plan")
    expect(plan.get("planSha256") == digest(content) and plan.get("manifestsSha256") == digest(manifests), "Reviewed plan or manifests changed")
    jobs = read_json(directory / "initialization-jobs.json")
    expect(plan.get("bootstrapSha256") == digest(jobs), "Initialization Job plan changed")
    validate_site(plan["site"])
    expect(plan["siteSha256"] == digest(plan["site"]), "Site input changed")
    check_no_credentials(manifests["items"])
    check_no_credentials(jobs["items"])
    expect(plan.get("resources") == [reference_key(item) for item in manifests["items"]], "Plan resource identity list differs")
    for item in manifests["items"]:
        kind, metadata = item.get("kind"), item.get("metadata", {})
        expect(kind in NAMESPACED_KINDS | CLUSTER_KINDS, "Unsupported planned resource kind")
        if kind in NAMESPACED_KINDS:
            expect(metadata.get("namespace") == plan["site"]["namespace"], "Plan contains a foreign namespace")
        else:
            expect(plan["site"]["security"]["includeClusterResources"] and metadata.get("name", "").startswith(plan["site"]["namespace"] + "-"), "Unapproved cluster resource scope")
        spec = pod_spec(item)
        if spec is not None:
            expect(not spec.get("nodeName"), "Plan inherited a nodeName")
            for container in spec.get("containers", []) + spec.get("initContainers", []):
                rewrite_image(container.get("image"), {})
    return plan, manifests


def identity(site):
    for command, expected in ((["whoami"], site["cluster"]["user"]), (["whoami", "--show-server"], site["cluster"]["server"]), (["config", "current-context"], site["cluster"]["context"])):
        expect(oc(command) == expected, "OpenShift identity, API or context does not match the reviewed site")


def controller_inventory(namespace):
    raw = json.loads(oc(["-n", namespace, "get", "deployment,statefulset,daemonset", "-o", "json"]))
    return {reference_key(item): item for item in raw.get("items", [])}


def owned_install(directory, plan):
    identity(plan["site"])
    receipt = read_json(directory / "create-receipt.json")
    expect(receipt.get("planSha256") == plan["planSha256"] and receipt.get("controllerUIDs"), "A matching actual creation receipt is required")
    current = controller_inventory(plan["site"]["namespace"])
    expect(set(current) == set(receipt["controllerUIDs"]), "Controller inventory changed from this installation")
    for key, uid in receipt["controllerUIDs"].items():
        expect(current[key]["metadata"].get("uid") == uid, "Created controller identity changed")
    return current


def preflight(plan, manifests):
    site = plan["site"]
    identity(site)
    namespace = site["namespace"]
    expect(oc(["get", "namespace", namespace, "-o", "name"]) == "namespace/" + namespace, "Create the target project and its credential references first")
    for name in sorted(set(site["storage"].values())):
        oc(["get", "storageclass", name, "-o", "name"])
    secret_names = set(site["auth"]["imagePullSecrets"]) | set(site["auth"]["secretNames"].values())
    for name in sorted(secret_names):
        # Metadata only. Do not request JSON/YAML or Secret data.
        expect(oc(["-n", namespace, "get", "secret", name, "-o", "jsonpath={.metadata.name}"]) == name, "A required existing Secret reference is absent")
    current = json.loads(oc(["-n", namespace, "get", "deployment,statefulset,daemonset,persistentvolumeclaim,job", "-o", "json"]))
    expect(not current.get("items"), "Target is not a fresh namespace: controllers, PVCs or Jobs already exist; this tool does not upgrade or adopt them")
    for item in manifests["items"]:
        arguments = ["get", item["kind"], item["metadata"]["name"], "--ignore-not-found", "-o", "name"]
        if item["kind"] in NAMESPACED_KINDS:
            arguments = ["-n", namespace, *arguments]
        expect(not oc(arguments), "A planned resource already exists; no overwrite/adoption is permitted")
    return {"identity": "matched", "target": "fresh", "credentialValuesRead": False}


def review_or_apply(directory: Path, execute=False, approved=None):
    plan, manifests = load_plan(directory)
    if execute:
        expect(approved == plan["planSha256"], "Execution requires the exact reviewed --approved-plan-sha256")
    checked = preflight(plan, manifests)
    payload = json.dumps(manifests)
    oc(["create", "--dry-run=server", "-f", "-", "-o", "name"], data=payload)
    if not execute:
        return {"status": "dry-run-pass", "planSha256": plan["planSha256"], **checked, "resourcesCreated": False}
    # Never apply, replace, delete, prune or force conflicts. A partial create
    # failure requires inspection; automatic rollback must not destroy PVCs.
    created = oc(["create", "-f", "-", "-o", "name"], data=payload).splitlines()
    controllers = controller_inventory(plan["site"]["namespace"])
    expect(set(controllers) == set(plan["desiredReplicas"]), "Created controller inventory is incomplete; inspect partial installation")
    receipt = {"status": "created-not-functionally-accepted", "planSha256": plan["planSha256"],
               "created": created, "resourcesDeleted": False, "freshInstallAccepted": False,
               "controllerUIDs": {key: item["metadata"]["uid"] for key, item in controllers.items()}}
    write_json(directory / "create-receipt.json", receipt)
    return receipt


def initialize(directory: Path, execute=False, approved=None):
    plan, manifests = load_plan(directory)
    if execute: expect(approved == plan["planSha256"], "Execution requires the exact reviewed --approved-plan-sha256")
    current = owned_install(directory, plan)
    for key, item in current.items():
        expect(item["spec"].get("replicas", 1) == (plan["desiredReplicas"][key] if key in FOUNDATION else 0), "Applications must remain stopped until initialization and asset staging pass")
    jobs = read_json(directory / "initialization-jobs.json")
    expect(bool(jobs["items"]), "Fresh initialization Job recipes are missing")
    oc(["create", "--dry-run=server", "-f", "-", "-o", "name"], data=json.dumps(jobs))
    if not execute: return {"status": "initialization-dry-run-pass", "planSha256": plan["planSha256"], "resourcesCreated": False}
    namespace = plan["site"]["namespace"]
    for key in sorted(FOUNDATION & set(current)):
        oc(["-n", namespace, "rollout", "status", key, "--timeout=240s"])
    completed = []
    for job in jobs["items"]:
        name = job["metadata"]["name"]
        # Never replace or delete a prior Job to manufacture success.
        expect(not oc(["-n", namespace, "get", "job", name, "--ignore-not-found", "-o", "name"]), "Initialization Job already exists; inspect its status instead of recreating it")
        oc(["create", "-f", "-", "-o", "name"], data=json.dumps(job))
        oc(["-n", namespace, "wait", "--for=condition=complete", "job/" + name, "--timeout=240s"])
        completed.append(name)
    receipt = {"status": "pass", "planSha256": plan["planSha256"], "completedJobs": completed,
               "freshInstallAccepted": False}
    write_json(directory / "initialization-receipt.json", receipt)
    return receipt


def activate(directory: Path, execute=False, approved=None):
    plan, manifests = load_plan(directory)
    if execute: expect(approved == plan["planSha256"], "Execution requires the exact reviewed --approved-plan-sha256")
    current = owned_install(directory, plan)
    initialization = read_json(directory / "initialization-receipt.json")
    assets = read_json(directory / "assets-receipt.json")
    expect(initialization.get("status") == "pass" and initialization.get("planSha256") == plan["planSha256"], "Fresh initialization has not passed for this plan")
    expect(assets.get("status") == "pass" and assets.get("planSha256") == plan["planSha256"] and assets.get("pvcHashesVerified") is True
           and assets.get("helperPodRemoved") is True and assets.get("modelSha256") == "0a22264542514149bead6e8582499d9758d51e3fde2892d9d2cc378a60426267", "Verified source/model population and helper cleanup are required")
    expect(assets.get("mediaManifest", {}).get("secondsPerCamera") == plan["sourceMedia"]["seconds"], "Populated source media duration differs")
    expect(set(assets.get("claimUids", {})) == {"warehouse-source-videos", "warehouse-perception-models"}, "Asset receipt lacks exact source/model PVC identities")
    for name, uid in assets["claimUids"].items():
        expect(oc(["-n", plan["site"]["namespace"], "get", "pvc", name, "-o", "jsonpath={.metadata.uid}"]) == uid, "An asset PVC was replaced after verified population")
    operations = []
    for key, desired in plan["desiredReplicas"].items():
        if key in FOUNDATION or not desired: continue
        expect(current[key]["spec"].get("replicas", 1) == 0, "Application already active; do not adopt another operator's activation")
        operations.append((key, desired))
    if not execute: return {"status": "activation-plan", "operations": operations, "sourceRemainsStopped": True}
    namespace = plan["site"]["namespace"]
    for key, desired in operations:
        oc(["-n", namespace, "scale", key, "--replicas=" + str(desired)])
    receipt = {"status": "activation-requested", "planSha256": plan["planSha256"], "operations": operations,
               "sourceRemainsStopped": True, "freshInstallAccepted": False}
    write_json(directory / "activation-receipt.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    inventory = sub.add_parser("inventory", help="Offline accepted-baseline gap report")
    inventory.add_argument("--baseline", type=Path, required=True)
    for name in ("render", "preview"):
        renderer = sub.add_parser(name, help="Offline site-bound rendering; preview never creates an executable install plan")
        renderer.add_argument("--baseline", type=Path, required=True)
        renderer.add_argument("--site", type=Path, required=True)
        renderer.add_argument("--output", type=Path, required=True)
    for name in ("dry-run", "apply", "initialize", "activate"):
        command = sub.add_parser(name)
        command.add_argument("--plan-directory", type=Path, required=True)
        if name != "dry-run":
            command.add_argument("--execute", action="store_true")
            command.add_argument("--approved-plan-sha256")
    arguments = parser.parse_args()
    try:
        if arguments.command == "inventory":
            baseline = read_json(arguments.baseline)
            gaps = baseline_gaps(baseline)
            result = {"status": "blocked" if gaps else "reviewed", "gaps": gaps,
                      "resources": len(baseline.get("resources", [])), "clusterAccess": False}
        elif arguments.command == "render":
            result = render(read_json(arguments.baseline), read_json(arguments.site), arguments.output)
        elif arguments.command == "preview":
            result = preview(read_json(arguments.baseline), read_json(arguments.site), arguments.output)
        elif arguments.command == "initialize":
            result = initialize(arguments.plan_directory, arguments.execute, arguments.approved_plan_sha256)
        elif arguments.command == "activate":
            result = activate(arguments.plan_directory, arguments.execute, arguments.approved_plan_sha256)
        else:
            result = review_or_apply(arguments.plan_directory, getattr(arguments, "execute", False), getattr(arguments, "approved_plan_sha256", None))
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"FAIL: {error}\n")


if __name__ == "__main__":
    main()
