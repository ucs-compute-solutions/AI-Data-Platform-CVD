#!/usr/bin/env python3
"""Normalize a sanitized runtime capture into a non-deployable review baseline.

No cluster calls are made. Redactions are retained as gaps, never replaced by
guessed defaults. The output cannot be installed until reconstruction review.
"""
from __future__ import annotations

import argparse
import copy
import ipaddress
import json
import re
import urllib.parse
from pathlib import Path

import warehouse_deploy as deploy

IP = re.compile(r"(?<![A-Za-z0-9_.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?:/[0-9]{1,2})?(?![A-Za-z0-9_.])")
PRIVATE_HOST = re.compile(r"\b(?:[A-Za-z0-9_-]+\.)+[A-Za-z0-9_-]+\.local\b", re.I)
ROLES = {"nvstreamer": "Deployment/warehouse-nvstreamer", "viosStreamprocessing": "StatefulSet/warehouse-vios-streamprocessing",
         "rtviCv": "StatefulSet/warehouse-rtvi-cv", "rtviVlm": "Deployment/warehouse-rtvi-vlm", "nemotron": "Deployment/warehouse-nemotron-nano",
         "viosSensor": "Deployment/warehouse-vios-sensor", "alertBridge": "Deployment/warehouse-alert-bridge",
         "vaMcp": "Deployment/warehouse-va-mcp", "agent": "Deployment/warehouse-demo-agent", "ui": "Deployment/warehouse-demo-ui",
         "kafka": "StatefulSet/warehouse-kafka", "postgres": "StatefulSet/warehouse-vios-postgres", "elasticsearch": "StatefulSet/warehouse-elasticsearch"}
DROP_ANNOTATIONS = ("kubectl.kubernetes.io/", "deployment.kubernetes.io/", "meta.helm.sh/", "pv.kubernetes.io/", "volume.kubernetes.io/")


def tokenize_service_domain(value):
    """Bind captured Kubernetes service FQDNs to the target DNS domain."""
    return re.sub(r"(?<=\.svc\.)cluster\.local(?![A-Za-z0-9_.-])",
                  "@@CLUSTER_DOMAIN@@", value)


def metadata(value, root=False):
    result = {key: copy.deepcopy(child) for key, child in value.items() if key in {"name", "namespace", "labels", "annotations"}}
    if "annotations" in result:
        result["annotations"] = {key: child for key, child in result["annotations"].items() if not key.startswith(DROP_ANNOTATIONS)}
        if not result["annotations"]: result.pop("annotations")
    if root and "labels" in result:
        result["labels"].pop("app.kubernetes.io/managed-by", None)
        result["labels"].pop("warehouse.nvidia.com/site-overlay", None)
    return result


def normalize_resource(resource):
    result = copy.deepcopy(resource)
    result.pop("status", None)
    result["metadata"] = metadata(result.get("metadata", {}), True)
    result.pop("immutable", None) if resource.get("kind") == "ServiceAccount" else None
    if result.get("kind") == "ServiceAccount":
        result.pop("secrets", None)
        result.pop("imagePullSecrets", None)
    spec = deploy.pod_spec(result)
    if spec is not None:
        result["spec"]["template"]["metadata"] = metadata(result["spec"]["template"].get("metadata", {}))
        spec.pop("nodeName", None)
        spec.pop("nodeSelector", None)
        # This field is controller-generated and would retain an old revision.
        result["spec"].pop("revisionHistoryLimit", None)
        for claim in result["spec"].get("volumeClaimTemplates", []):
            claim["metadata"] = metadata(claim.get("metadata", {}))
            claim["metadata"].pop("namespace", None)
            claim.pop("status", None)
            claim["spec"].pop("volumeName", None)
    if result.get("kind") == "Service":
        service = result["spec"]
        if service.get("clusterIP") != "None": service.pop("clusterIP", None)
        for key in ("clusterIPs", "ipFamilies", "ipFamilyPolicy", "healthCheckNodePort"):
            service.pop(key, None)
        for port in service.get("ports", []): port.pop("nodePort", None)
    if result.get("kind") == "PersistentVolumeClaim":
        result["spec"].pop("volumeName", None)
        result["spec"].pop("selector", None)
    if result.get("kind") == "Route":
        result["spec"].pop("host", None)
        for key in ("certificate", "key", "caCertificate", "destinationCACertificate"):
            if key in result.get("spec", {}).get("tls", {}):
                result["spec"]["tls"][key] = "[REDACTED]"
    return result


def restore_verified_empty_fields(capture, supplement, namespace):
    """Recover exact empty values from type/empty-only observations, not secrets."""
    capture = copy.deepcopy(capture)
    by_name = {item["metadata"]["name"]: item for item in capture["namespaces"][namespace]["items"] if item.get("kind") == "ConfigMap"}
    restored = 0
    for evidence in supplement.get("optionalCredentialShapes", []):
        deploy.expect(evidence.get("isEmpty") is True and evidence.get("verifiedEmptyValue") in ("", []), "Only independently verified empty optional fields may be reconstructed")
        item = by_name[evidence["configMap"]]
        data = json.loads(item["data"][evidence["dataKey"]])
        parent = data
        for part in evidence["path"][:-1]: parent = parent[part]
        final = evidence["path"][-1]
        deploy.expect(parent[final] == "[REDACTED]", "Empty-shape evidence no longer matches the captured redaction")
        parent[final] = evidence["verifiedEmptyValue"]
        item["data"][evidence["dataKey"]] = json.dumps(data, indent=2) + "\n"
        restored += 1
    postgres = by_name.get("warehouse-vios-postgres-secure-v1")
    if postgres and "[REDACTED]" in postgres["data"].get("postgresql.conf", ""):
        # Same nonsecret algorithm as the checked-in source-locked native
        # PostgreSQL configuration. Never reconstruct an actual password.
        text, count = re.subn(r"(?m)^password_encryption\s*=\s*\[REDACTED\]\s*$", "password_encryption = 'scram-sha-256'", postgres["data"]["postgresql.conf"])
        deploy.expect(count == 1, "Unexpected PostgreSQL redaction; require source comparison")
        postgres["data"]["postgresql.conf"] = text
    return capture, restored


def derive(capture, namespace, supplement=None, *, reviewed_candidate=False):
    empty_fields = 0
    if supplement:
        capture, empty_fields = restore_verified_empty_fields(capture, supplement, namespace)
    items = capture["namespaces"][namespace]["items"]
    by_key = {deploy.reference_key(item): item for item in items}
    controllers = [item for item in items if item.get("kind") in deploy.CONTROLLERS and not item["metadata"]["name"].startswith("warehouse-csi-")]
    referenced_cm, referenced_pvc, accounts = {"warehouse-kafka-topics", "warehouse-elasticsearch-init", "warehouse-kibana-dashboard-2d"}, set(), set()
    for item in controllers:
        spec = deploy.pod_spec(item)
        accounts.add(spec.get("serviceAccountName", "default"))
        for _, value in deploy.walk(item):
            if not isinstance(value, dict): continue
            for field in ("configMap", "configMapRef", "configMapKeyRef"):
                if isinstance(value.get(field), dict): referenced_cm.add(value[field].get("name"))
            if isinstance(value.get("persistentVolumeClaim"), dict): referenced_pvc.add(value["persistentVolumeClaim"].get("claimName"))
        for claim in item.get("spec", {}).get("volumeClaimTemplates", []):
            for ordinal in range(item.get("spec", {}).get("replicas", 1)):
                referenced_pvc.add(f"{claim['metadata']['name']}-{item['metadata']['name']}-{ordinal}")
    selected = list(controllers)
    for item in items:
        kind, name = item.get("kind"), item.get("metadata", {}).get("name", "")
        if ((kind == "ConfigMap" and name in referenced_cm)
                or (kind == "PersistentVolumeClaim" and name in referenced_pvc)
                or (kind == "ServiceAccount" and name in accounts and name.startswith("warehouse-"))
                or (kind in {"Service", "NetworkPolicy", "Route"} and name.startswith("warehouse-") and not name.startswith("warehouse-csi-"))
                or (kind == "RoleBinding" and name.startswith("warehouse-"))):
            selected.append(item)
    service_ips = {item["spec"].get("clusterIP"): item["metadata"]["name"] + ".@@NAMESPACE@@.svc.@@CLUSTER_DOMAIN@@"
                   for item in items if item.get("kind") == "Service" and item.get("spec", {}).get("clusterIP") not in {None, "None"}}
    actual_addresses = {value for item in items if item.get("kind") == "Pod" for key in ("podIP", "hostIP")
                        for value in [item.get("status", {}).get(key)] if value}
    actual_addresses.update(address.get("address") for node in capture.get("nodes", {}).get("items", [])
                            for address in node.get("status", {}).get("addresses", []) if address.get("type") in {"InternalIP", "ExternalIP"})
    public_hosts = {item["spec"].get("host") for item in items if item.get("kind") == "Route" and item.get("spec", {}).get("host")}
    registry_hosts = {container["image"].split("/", 1)[0] for item in controllers for container in deploy.pod_spec(item).get("containers", [])
                      if ".local/" in container.get("image", "")}
    network_tokens, uses, redactions = {}, {}, []

    def network_value(value, path):
        def address(match):
            original = match.group(0)
            try:
                if "/" in original: ipaddress.ip_network(original, strict=False)
                else: ipaddress.ip_address(original)
            except ValueError: return original
            if original in {"0.0.0.0", "127.0.0.1", "255.255.255.255", "0.0.0.0/0"}: return original
            if original in service_ips: return service_ips[original]
            # Do not rewrite example addresses in upstream comments, report
            # examples or Redis documentation. Only actual captured addresses
            # and explicit NetworkPolicy CIDRs are deployment parameters.
            if original not in actual_addresses and not path.endswith("/ipBlock/cidr"):
                return original
            category = "NET_CIDR" if "/" in original else "NET_ADDRESS"
            pair = (category, original)
            if pair not in network_tokens:
                network_tokens[pair] = category + "_" + str(1 + sum(key[0] == category for key in network_tokens))
            token = network_tokens[pair]
            uses.setdefault(token, set()).add(path)
            return "@@" + token + "@@"
        return IP.sub(address, value)

    def transform(value, path=""):
        if isinstance(value, dict): return {key: transform(child, path + "/" + key) for key, child in value.items()}
        if isinstance(value, list): return [transform(child, path + "/" + str(index)) for index, child in enumerate(value)]
        if not isinstance(value, str): return value
        if "[REDACTED]" in value: redactions.append(path)
        for host in public_hosts: value = value.replace(host, "@@PUBLIC_HOST@@")
        for host in registry_hosts: value = value.replace(host, "registry.example.com")
        value = value.replace(namespace, "@@NAMESPACE@@")
        value = value.replace("warehouse-fixed-uid-runtime-default", "@@SCC_NAME@@")
        value = value.replace("warehouse-scc-fixed-uid-runtime-default", "@@CLUSTER_ROLE_NAME@@")
        value = tokenize_service_domain(value)
        value = network_value(value, path)
        # Any other captured private DNS dependency needs deliberate review.
        for host in PRIVATE_HOST.findall(value):
            if host == "svc.cluster.local" or host.endswith(".svc.cluster.local"):
                continue
            pair = ("NET_ADDRESS", host)
            if pair not in network_tokens:
                network_tokens[pair] = "NET_ADDRESS_" + str(1 + sum(key[0] == "NET_ADDRESS" for key in network_tokens))
            token = network_tokens[pair]
            uses.setdefault(token, set()).add(path)
            value = value.replace(host, "@@" + token + "@@")
        return value

    resources = [transform(normalize_resource(item), deploy.reference_key(item)) for item in selected]
    # Retained-data promotion required PG_VERSION and a legacy database. A new
    # empty volume needs an explicit different bootstrap; never rename user data.
    postgres = next(item for item in resources if deploy.reference_key(item) == ROLES["postgres"])
    for volume in deploy.pod_spec(postgres).get("volumes", []):
        if volume.get("configMap", {}).get("name") == "warehouse-vios-postgres-migration-v1":
            volume["configMap"]["name"] = "warehouse-postgres-fresh-bootstrap-v1"
    migration = next(item for item in deploy.pod_spec(postgres)["initContainers"] if item["name"] == "vios-postgres-migration-init")
    migration["name"] = "vios-postgres-fresh-bootstrap"
    migration["command"] = ["/bin/sh", "/opt/warehouse/migrate-vios-postgres.sh"]
    migration["env"] = [item for item in migration["env"] if item["name"] in {"PG_ADMIN_PASSWORD", "PG_APP_PASSWORD"}]
    resources = [item for item in resources if deploy.reference_key(item) != "ConfigMap/warehouse-vios-postgres-migration-v1"]
    resources.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "warehouse-postgres-fresh-bootstrap-v1", "namespace": "@@NAMESPACE@@"},
                      "immutable": True, "data": {"migrate-vios-postgres.sh": Path(__file__).with_name("fresh-postgres-bootstrap.sh").read_text()}})
    if supplement:
        for collection, name in (("sccs", "warehouse-fixed-uid-runtime-default"),
                                 ("clusterRoles", "warehouse-scc-fixed-uid-runtime-default")):
            matches = [item for item in supplement.get(collection, []) if item.get("metadata", {}).get("name") == name]
            deploy.expect(len(matches) == 1, "Required accepted custom security definition is missing")
            resources.append(transform(normalize_resource(matches[0]), collection + "/" + name))
    values = capture["helm"][namespace]["values"]["nvidia-vss-warehouse"]
    jobs = []
    config_names = {"kafkaTopics": "warehouse-kafka-topics", "kibanaDashboard2d": "warehouse-kibana-dashboard-2d"}
    for key in ("kafkaTopics", "elasticsearchInit", "kibanaDashboard"):
        recipe = values["initializationJobs"][key]
        deploy.expect(recipe["implementationStatus"] == "render-ready" and recipe.get("secretEnv") in ([], "[REDACTED]"), "Bootstrap recipe needs further source reconstruction")
        # These three pinned native initialization recipes have empty
        # secretEnv arrays in the checked-in source. Capture filtering masks
        # the key itself; no credential value is recovered or defaulted here.
        image = values["images"][recipe["image"]]
        digest = image["digest"]
        if key == "kafkaTopics":
            # The recorded .1 helper was superseded by this random-UID fix.
            digest = "sha256:09b17f0a12ee966ef99804a51af8ae0c1d21173852fb88fc5185b01fa468f8a3"
        container = {"name": "initialize", "image": image["repository"] + "@" + digest,
                     "command": recipe["command"], "args": recipe.get("args", []), "resources": recipe["resources"],
                     "envFrom": [{"configMapRef": {"name": "warehouse-source-lock"}}],
                     "env": [{"name": name, "value": str(value)} for name, value in recipe.get("env", {}).items()],
                     "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}}
        volumes, mounts = [], []
        for mount in recipe.get("configMapMounts", []):
            name = config_names[mount["configMap"]]
            volume = "config-" + name
            if not any(item["name"] == volume for item in volumes): volumes.append({"name": volume, "configMap": {"name": name}})
            mounts.append({"name": volume, "mountPath": mount["mountPath"], "subPath": mount["subPath"], "readOnly": True})
        if mounts: container["volumeMounts"] = mounts
        job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "warehouse-initialize-" + key.lower(), "namespace": namespace},
               "spec": {"backoffLimit": 1, "activeDeadlineSeconds": recipe["activeDeadlineSeconds"],
                        "template": {"metadata": {"labels": {"warehouse.nvidia.com/bootstrap": "true"}},
                                     "spec": {"restartPolicy": "Never", "serviceAccountName": "warehouse-general", "automountServiceAccountToken": False,
                                              "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                                              "containers": [container], "volumes": volumes}}}}
        jobs.append(transform(job, "bootstrap/" + key))
    # Fresh initializer Pods have their own explicit network path. Do not rely
    # on old one-shot Job selectors or weaken the application policies.
    boot_label = {"warehouse.nvidia.com/bootstrap": "true"}
    resources.append({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "warehouse-initialize-egress", "namespace": "@@NAMESPACE@@"},
                      "spec": {"podSelector": {"matchLabels": boot_label}, "policyTypes": ["Egress"], "egress": [
                          {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "openshift-dns"}}}], "ports": [{"protocol": protocol, "port": port} for protocol in ("TCP", "UDP") for port in (53, 5353)]},
                          {"to": [{"podSelector": {}}], "ports": [{"protocol": "TCP", "port": port} for port in (9092, 9200, 5601)]}]}})
    for role in ("kafka", "elasticsearch"):
        target = next(item for item in resources if deploy.reference_key(item) == ROLES[role])
        selector = target["spec"]["selector"]["matchLabels"]
        resources.append({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "warehouse-initialize-to-" + role, "namespace": "@@NAMESPACE@@"},
                          "spec": {"podSelector": {"matchLabels": selector}, "policyTypes": ["Ingress"], "ingress": [{"from": [{"podSelector": {"matchLabels": boot_label}}], "ports": [{"protocol": "TCP", "port": 9092 if role == "kafka" else 9200}]}]}})
    kibana = next(item for item in resources if deploy.reference_key(item) == "Deployment/warehouse-kibana")
    resources.append({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "warehouse-initialize-to-kibana", "namespace": "@@NAMESPACE@@"},
                      "spec": {"podSelector": {"matchLabels": kibana["spec"]["selector"]["matchLabels"]}, "policyTypes": ["Ingress"], "ingress": [{"from": [{"podSelector": {"matchLabels": boot_label}}], "ports": [{"protocol": "TCP", "port": 5601}]}]}})
    image_catalog = sorted({container["image"] for item in resources + jobs if deploy.pod_spec(item) is not None
                            for container in deploy.pod_spec(item).get("containers", []) + deploy.pod_spec(item).get("initContainers", [])})
    # A redacted local placeholder key cannot become a publishable literal.
    secret_env = []
    for item in resources:
        if deploy.pod_spec(item) is None: continue
        for container in deploy.pod_spec(item).get("containers", []):
            for entry in container.get("env", []):
                if entry.get("name") == "OPENAI_API_KEY" and entry.get("value") == "[REDACTED]":
                    entry.pop("value")
                    entry["valueFrom"] = {"secretKeyRef": {"name": "warehouse-agent-api", "key": "api-key"}}
                    secret_env.append(deploy.reference_key(item) + "/" + container["name"] + "/OPENAI_API_KEY")
    components = {role: key for role, key in ROLES.items() if key in by_key}
    baseline = {"format": 1, "kind": "warehouse-runtime-template", "status": "captured-unreviewed",
                "secretReviewComplete": False, "captureSha256": deploy.digest(capture),
                "upstream": {"tag": "v3.2.1", "commit": "7640d917047cf7b0fd3085eefb8282754b56bc94",
                             "nvstreamerBaseCommit": "ac7d872f1a5ba5e5641562c0aa9a4a37138da390"},
                "components": components, "corrections": {}, "bootstrap": {"reviewed": False, "jobs": jobs,
                    "freshPostgres": "warehouse-postgres-fresh-bootstrap-v1", "calibration": "native Configurator; verify five datasets after application activation"},
                "forbiddenSourceStrings": [], "resources": resources}
    if supplement:
        baseline["supplementSha256"] = deploy.digest(supplement)
    remaining_redactions = ["/".join(path) for path, value in deploy.walk(resources) if isinstance(value, str) and "[REDACTED]" in value]
    inventory = {"format": 1, "status": "reconstruction-required", "resourceCount": len(resources),
                 "counts": {kind: sum(item["kind"] == kind for item in resources) for kind in sorted({item["kind"] for item in resources})},
                 "redactedFields": sorted(remaining_redactions), "verifiedEmptyFieldsRestored": empty_fields,
                 "requiredNetworkTokens": {key: sorted(value) for key, value in sorted(uses.items())},
                 "networkTokenMeanings": {key: (
                     "Current router/OVN-SNAT source address (/32) observed by application ingress; discover for the target cluster"
                     if any("/ingress/" in path for path in paths) else
                     "Kubernetes Service IP (/32), HTTPS TCP 443" if any("/egress/4/" in path for path in paths) else
                     "Control-plane API endpoint (/32), HTTPS TCP 6443" if any("/egress/5/" in path for path in paths) else
                     "Review the listed policy/native configuration usage against target networking") for key, paths in sorted(uses.items())},
                 "agentCredentialConvertedToExistingSecretRef": secret_env,
                 "requiredSecretNames": sorted({value[field].get("name", value[field].get("secretName"))
                     for item in resources for _, value in deploy.walk(item) if isinstance(value, dict)
                     for field in ("secretKeyRef", "secretRef", "secret") if isinstance(value.get(field), dict)}),
                 "routeNames": sorted(item["metadata"]["name"] for item in resources if item["kind"] == "Route"),
                 "missingConfigMaps": sorted(name for name in referenced_cm if "ConfigMap/" + name not in by_key),
                 "missingPVCs": sorted(name for name in referenced_pvc if "PersistentVolumeClaim/" + name not in by_key),
                 "openItems": ["Review every native redaction and reconstruct only from nonsecret source or supported Secret injection",
                               "Capture/compare referenced cluster-scoped SCC and ClusterRole definitions",
                               "Verify complete correction coverage and imported derived-image availability",
                               "Provide clean database/index/model/video initialization with fresh camera/calibration ID mapping",
                               "Run fresh-site acceptance; this capture is not a completed install"]}
    if reviewed_candidate:
        deploy.expect(supplement and not remaining_redactions and not inventory["missingConfigMaps"] and not inventory["missingPVCs"], "Candidate review requires the complete, credential-reviewed supplement and reference closure")
        indexed = {deploy.reference_key(item): item for item in resources}
        coverage = {
            "vios-shared-listener-v2": ["ConfigMap/warehouse-vios-shared-listener-v2", "Service/warehouse-vios-streamprocessing"],
            "vios-remote-postgresql": ["ConfigMap/warehouse-vios-remote-db-tcp-canary-v1", ROLES["postgres"]],
            "vios-uid1000-runtime": [ROLES["viosStreamprocessing"], ROLES["viosSensor"]],
            "rtvi-cv-driver-libraries-launcher": [ROLES["rtviCv"], "ConfigMap/warehouse-rtvi-cv-launcher-r2"],
            "rtsp-tcp-four-camera": ["ConfigMap/warehouse-deepstream-2d-four-camera-reference-r3", "ConfigMap/warehouse-vios-remote-db-tcp-canary-v1"],
            "nvstreamer-derived-p4": [ROLES["nvstreamer"]], "rtvi-vlm-release-mode": [ROLES["rtviVlm"]],
            "stateful-kafka-probes-dns": [ROLES["kafka"], "ConfigMap/warehouse-kafka-runtime"],
            "ui-sensor-id": [ROLES["ui"]], "ui-report-conversation-isolation": [ROLES["ui"]],
            "agent-report-source-binding": ["ConfigMap/warehouse-agent-bound-report-v1", ROLES["agent"]],
            "va-mcp-vlm-incident-lookup": ["ConfigMap/warehouse-va-mcp-rule-lookup-v1", ROLES["vaMcp"]],
            "agent-incident-followup": ["ConfigMap/warehouse-agent-incident-followup-v1", ROLES["agent"]],
            "complete-network-flows": [deploy.reference_key(item) for item in resources if item["kind"] == "NetworkPolicy"],
        }
        for ident, keys in coverage.items():
            deploy.expect(keys and all(key in indexed for key in keys), "Captured correction source is missing: " + ident)
            baseline["corrections"][ident] = {"status": "verified", "scope": "captured declarative inputs and reconstruction, not fresh-cluster runtime",
                "resources": keys, "evidenceSha256": deploy.digest([indexed[key] for key in keys]), "freshClusterRuntimeVerified": False}
        baseline["status"] = "reviewed-runtime-candidate"
        baseline["secretReviewComplete"] = True
        baseline["bootstrap"]["reviewed"] = True
        baseline["bootstrap"]["scope"] = "source-contract review; runtime acceptance pending"
        baseline["bootstrap"]["runtimeAccepted"] = False
        baseline["freshInstallAccepted"] = False
        baseline["validationRequired"] = ["Empty-volume PostgreSQL initialization and application-role authentication",
            "Native Kafka/Elasticsearch/Kibana initialization Jobs", "Model/source PVC population and GPU engine/cache creation",
            "Discovered-ID camera bootstrap, four advancing CV streams and Kafka metadata", "UI alert, clip, incident-bound report/follow-up",
            "Short cold/warm restarts, finite hour and verified cleanup", "Clean standalone-checkout repeat"]
        deploy.check_no_credentials(resources + jobs)
        inventory["status"] = "source-reviewed-candidate-runtime-pending"
        inventory["openItems"] = baseline["validationRequired"]
    return baseline, inventory, image_catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--source-namespace", required=True)
    parser.add_argument("--supplement", type=Path, help="Verified type/empty-only fields and accepted custom SCC/ClusterRole")
    parser.add_argument("--reviewed-candidate", action="store_true", help="Record completed source reconstruction review; never claims fresh-install runtime acceptance")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        deploy.expect(not args.output.exists(), "Output already exists; choose a new review directory")
        baseline, inventory, images = derive(deploy.read_json(args.capture), args.source_namespace,
                                             deploy.read_json(args.supplement) if args.supplement else None,
                                             reviewed_candidate=args.reviewed_candidate)
        serialized = json.dumps(baseline)
        source_api = urllib.parse.urlsplit(deploy.read_json(args.capture).get("identity", {}).get("api", "")).hostname
        deploy.expect((not source_api or source_api not in serialized) and args.source_namespace not in serialized, "A source-site identifier remains; inspect privately before publishing")
        args.output.mkdir(parents=True, exist_ok=False)
        for name, value in (("baseline.captured.json", baseline), ("capture-inventory.json", inventory), ("images.json", images)):
            deploy.write_json(args.output / name, value)
        example = deploy.read_json(Path(__file__).with_name("site.example.json"))
        example["ingress"]["routeHosts"] = {name: example["ingress"]["publicHost"] for name in inventory["routeNames"]}
        for index, token in enumerate(inventory["requiredNetworkTokens"], 1):
            if token.startswith("NET_CIDR_"):
                example["network"]["cidrs"][token] = f"192.0.2.{index}/32"
            else:
                example["network"]["addresses"][token] = f"endpoint-{index}.example.com"
        for image in images:
            repository = image.split("@", 1)[0]
            if repository.startswith("registry.example.com/"):
                example["images"]["repositoryMirrors"][repository] = repository.replace("registry.example.com/", "registry.example.net/")
        example["security"]["includeClusterResources"] = True
        deploy.write_json(args.output / "site.render-example.json", example)
        print(json.dumps({"status": baseline["status"], "resources": len(baseline["resources"]), "redactedFields": len(inventory["redactedFields"]), "images": len(images), "freshInstallAccepted": False}, indent=2))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, "FAIL: " + str(error) + "\n")


if __name__ == "__main__":
    main()
