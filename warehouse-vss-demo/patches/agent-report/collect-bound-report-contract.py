#!/usr/bin/env python3
"""Print a narrow, read-only CPU Agent review contract without source or secrets.

Run on the OpenShift administration client. Only deployment identifiers,
image/resources, ConfigMap-backed report/config mounts and file SHA-256 values
are returned. No report request, source-file output or cluster change occurs.
"""

import argparse
import json
import re
import subprocess
import sys


PATHS = {
    "report_agent.py": "/vss-agent/.venv/lib/python3.13/site-packages/vss_agents/agents/report_agent.py",
    "template_report_gen.py": "/vss-agent/.venv/lib/python3.13/site-packages/vss_agents/tools/template_report_gen.py",
    "config.yml": "/vss-agent/config/config.yml",
    "incident_report_template.md": "/vss-agent/config/incident_report_template.md",
    "va_mcp_server_config.yml": "/vss-agent/config/va_mcp_server_config.yml",
}


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=30).stdout


def collect(namespace, deployment_name):
    deployment = json.loads(run("oc", "-n", namespace, "get", "deployment", deployment_name, "-o", "json"))
    spec = deployment["spec"]["template"]["spec"]
    container = next(x for x in spec["containers"] if x["name"] == "agent")
    code = "import hashlib,json; p=" + repr(PATHS) + "; print(json.dumps({k:hashlib.sha256(open(v,'rb').read()).hexdigest() for k,v in p.items()},sort_keys=True))"
    hashes = json.loads(run("oc", "-n", namespace, "exec", "deployment/" + deployment_name, "-c", "agent", "--", "/usr/local/bin/python3", "-c", code))
    mounts = [x for x in container.get("volumeMounts", []) if x.get("mountPath") in PATHS.values() or x.get("mountPath") == "/vss-agent/config"]
    mount_names = {x["name"] for x in mounts}
    volumes = [{"name": x["name"], "configMap": x["configMap"]} for x in spec.get("volumes", []) if x["name"] in mount_names and "configMap" in x]
    return {
        "namespace": namespace, "deployment": deployment_name,
        "uid": deployment["metadata"]["uid"], "resourceVersion": deployment["metadata"]["resourceVersion"],
        "image": container["image"], "resources": container.get("resources", {}), "nodeName": spec.get("nodeName"),
        "volumeMounts": mounts, "configMapVolumes": volumes, "runtime_sha256": hashes,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="nvidia-vss-321-warehouse")
    parser.add_argument("--deployment", default="warehouse-demo-agent")
    args = parser.parse_args()
    for value in (args.namespace, args.deployment):
        if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?", value):
            parser.error("Namespace and deployment must be valid Kubernetes names")
    try:
        contract = collect(args.namespace, args.deployment)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, StopIteration):
        print("FAIL: unable to collect the narrow read-only Agent contract", file=sys.stderr)
        return 1
    print(json.dumps(contract, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
