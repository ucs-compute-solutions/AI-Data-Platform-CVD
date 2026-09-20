#!/usr/bin/env python3
"""Render offline, shutdown-by-default Nexus candidate configurations."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined


ROOT = Path(__file__).resolve().parent
TEMPLATE_DIR = ROOT / "templates"
EXPECTED_PLATFORM = "N9K-C9332D-GX2B"
FORBIDDEN_MARKERS = ("REQUIRED", "TBD", "CHANGEME")
SECRET_KEY = re.compile(
    r"(?:password|passphrase|secret|token|api[_-]?key|private[_-]?key|"
    r"ssh[_-]?key|community|credential|certificate)",
    re.IGNORECASE,
)
HOSTNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$")
INTERFACE = re.compile(r"^Ethernet1/(\d+)(?:/(\d+))?$")
EXPECTED_VLANS = {
    10: "discovery-vlan",
    69: "storage-vlan69",
    1080: "oob-mgmt",
    1081: "inband-network",
    1082: "inband-network-2",
    3056: "VAST-Client_VLAN_3056",
    3057: "provisioning-ocp-3057",
}


class ValidationError(ValueError):
    """Raised when rendering inputs fail a safety or topology check."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render four offline CVD Nexus candidate configurations."
    )
    parser.add_argument("--values", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def walk(value: Any, path: str = "root") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if SECRET_KEY.search(str(key)):
                raise ValidationError(f"secret-like key is not permitted: {path}.{key}")
            walk(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            walk(item, f"{path}[{index}]")
    elif isinstance(value, str):
        upper = value.upper()
        if any(marker in upper for marker in FORBIDDEN_MARKERS):
            raise ValidationError(f"unresolved marker at {path}")
        if "9336" in upper:
            raise ValidationError(f"a third/9336 fabric is outside this CVD: {path}")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def interface_number(name: str) -> tuple[int, int | None]:
    match = INTERFACE.fullmatch(name)
    require(match is not None, f"unsupported interface name: {name}")
    assert match is not None
    return int(match.group(1)), int(match.group(2)) if match.group(2) else None


def breakout_range(first: str, last: str) -> list[str]:
    first_port, first_lane = interface_number(first)
    last_port, last_lane = interface_number(last)
    require(first_lane is not None and last_lane is not None, "storage role ranges must use breakout lanes")
    require(first_lane == 1 and last_lane == 2, "storage role ranges must start at lane 1 and end at lane 2")
    require(first_port <= last_port, "storage role range is reversed")
    return [
        f"Ethernet1/{port}/{lane}"
        for port in range(first_port, last_port + 1)
        for lane in (1, 2)
    ]


def validate_vlan_expression(expression: str, allowed: set[int], label: str) -> set[int]:
    found: set[int] = set()
    require(bool(expression), f"{label} has an empty VLAN list")
    for part in expression.split(","):
        part = part.strip()
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            require(start_text.isdigit() and end_text.isdigit(), f"invalid VLAN range in {label}: {part}")
            start, end = int(start_text), int(end_text)
            require(start <= end, f"reversed VLAN range in {label}: {part}")
            found.update(range(start, end + 1))
        else:
            require(part.isdigit(), f"invalid VLAN in {label}: {part}")
            found.add(int(part))
    require(found <= allowed, f"{label} contains VLANs outside the documented design: {sorted(found - allowed)}")
    require(1 not in found, f"{label} must not carry default VLAN 1")
    return found


def validate_switches(pair: dict[str, Any], label: str) -> None:
    switches = pair.get("switches")
    require(isinstance(switches, list) and len(switches) == 2, f"{label} must contain exactly two switches")
    require({item.get("role") for item in switches} == {"a", "b"}, f"{label} switch roles must be a and b")
    for switch in switches:
        require(bool(HOSTNAME.fullmatch(str(switch.get("hostname", "")))), f"invalid hostname in {label}")
        for field in ("keepalive_source", "keepalive_destination"):
            try:
                ipaddress.ip_address(switch[field])
            except (KeyError, ValueError) as exc:
                raise ValidationError(f"invalid {field} in {label}") from exc
        require(
            switch["keepalive_source"] != switch["keepalive_destination"],
            f"{label} keepalive source and destination must differ",
        )


def validate_port_channel(block: dict[str, Any], label: str, used: set[str]) -> None:
    pc = block.get("port_channel")
    require(isinstance(pc, int) and 1 <= pc <= 4096, f"invalid port-channel in {label}")
    members = block.get("members")
    require(isinstance(members, list) and members, f"{label} must include member interfaces")
    for member in members:
        interface_number(member)
        require(member not in used, f"interface {member} is reused in {label}")
        used.add(member)


def validate(data: dict[str, Any]) -> dict[str, Any]:
    walk(data)
    expected_top_level = {
        "site_name",
        "platform",
        "shutdown_by_default",
        "vlans",
        "qos",
        "storage_pair",
        "ai_gpu_ocp_pair",
    }
    require(
        set(data) == expected_top_level,
        "values must describe only the storage and AI/GPU/OpenShift switch pairs",
    )
    require(data.get("platform") == EXPECTED_PLATFORM, f"platform must be {EXPECTED_PLATFORM}")
    require(data.get("shutdown_by_default") is True, "shutdown_by_default must be true")

    common = data.get("vlans", {}).get("common", [])
    ai_only = data.get("vlans", {}).get("ai_only", [])
    vlan_items = common + ai_only
    vlan_map = {item.get("id"): item.get("name") for item in vlan_items}
    require(vlan_map == EXPECTED_VLANS, "VLAN IDs and names must match the documented CVD roles")
    require({item["id"] for item in ai_only} == {3057}, "VLAN 3057 must remain AI/OpenShift-only")
    allowed_common = {item["id"] for item in common}
    allowed_all = set(EXPECTED_VLANS)

    qos = data.get("qos", {})
    require(qos.get("mtu") == 9216, "the CVD MTU must be 9216")
    require(qos.get("pfc_cos") == 3, "the CVD PFC class must be CoS 3")

    storage = data.get("storage_pair")
    ai_pair = data.get("ai_gpu_ocp_pair")
    require(isinstance(storage, dict) and isinstance(ai_pair, dict), "exactly the storage and AI/OpenShift pairs are required")
    validate_switches(storage, "storage_pair")
    validate_switches(ai_pair, "ai_gpu_ocp_pair")
    require(storage.get("vpc_domain_id") != ai_pair.get("vpc_domain_id"), "vPC domain IDs must differ")

    roles = storage.get("port_roles", {})
    external = roles.get("external_northbound", {})
    internal = roles.get("storage_internal", {})
    require(
        (external.get("first"), external.get("last")) == ("Ethernet1/1/1", "Ethernet1/8/2"),
        "storage external/northbound range must be Ethernet1/1/1-Ethernet1/8/2",
    )
    require(
        (internal.get("first"), internal.get("last")) == ("Ethernet1/9/1", "Ethernet1/16/2"),
        "storage internal range must be Ethernet1/9/1-Ethernet1/16/2",
    )
    storage["external_interfaces"] = breakout_range(external["first"], external["last"])
    storage["internal_interfaces"] = breakout_range(internal["first"], internal["last"])
    require(set(storage["external_interfaces"]).isdisjoint(storage["internal_interfaces"]), "storage port role ranges overlap")
    validate_vlan_expression(external["allowed_vlans"], allowed_common, "storage external/northbound")
    validate_vlan_expression(internal["allowed_vlans"], allowed_common, "storage internal")

    for pair_name, pair, allowed in (
        ("storage_pair", storage, allowed_common),
        ("ai_gpu_ocp_pair", ai_pair, allowed_all),
    ):
        used: set[str] = set()
        for block_name in ("peer_link", "inter_fabric"):
            block = pair.get(block_name, {})
            validate_port_channel(block, f"{pair_name}.{block_name}", used)
            validate_vlan_expression(block["allowed_vlans"], allowed, f"{pair_name}.{block_name}")

    expected_inter_fabric_members = ["Ethernet1/24", "Ethernet1/25"]
    require(
        storage["inter_fabric"]["port_channel"] == 100
        and storage["inter_fabric"]["members"] == expected_inter_fabric_members,
        "storage Po100 must use Ethernet1/24-25 toward the matching AI/GPU switch",
    )
    require(
        ai_pair["inter_fabric"]["port_channel"] == 100
        and ai_pair["inter_fabric"]["members"] == expected_inter_fabric_members,
        "AI/GPU Po100 must use Ethernet1/24-25 toward the matching storage switch",
    )

    northbound = ai_pair.get("northbound", {})
    northbound_pc = northbound.get("port_channel")
    northbound_vpc = northbound.get("vpc_id")
    require(isinstance(northbound_pc, int) and 1 <= northbound_pc <= 4096, "invalid AI/OpenShift northbound port-channel")
    require(northbound_vpc == northbound_pc, "northbound vPC ID must match its port-channel ID")
    northbound_members = northbound.get("members", {})
    require(set(northbound_members) == {"a", "b"}, "northbound members must be defined for switch roles a and b")
    for role, members in northbound_members.items():
        require(isinstance(members, list) and members, f"northbound switch {role} requires member interfaces")
        for member in members:
            interface_number(member)
    validate_vlan_expression(northbound["allowed_vlans"], allowed_all, "ai_gpu_ocp_pair.northbound")

    hosts = ai_pair.get("hosts")
    require(
        isinstance(hosts, list) and len(hosts) == 5,
        "AI/OpenShift pair must define three C225 M8 nodes and the two separate C845A attachments",
    )
    kinds = [host.get("kind", "") for host in hosts]
    require(sum("C225 M8" in kind for kind in kinds) == 3, "AI/OpenShift pair must define three C225 M8 nodes")
    require(sum("C845A" in kind for kind in kinds) == 2, "AI/OpenShift pair must define CX-7 and X710 attachments for one C845A node")
    attachment_roles = [host.get("attachment_role") for host in hosts]
    require(
        attachment_roles.count("c845a-cx7") == 1
        and attachment_roles.count("c845a-x710-ocp") == 1,
        "C845A attachments must include one CX-7 data path and one X710 OpenShift path",
    )
    host_pcs: set[int] = {
        ai_pair["peer_link"]["port_channel"],
        ai_pair["inter_fabric"]["port_channel"],
        northbound_pc,
    }
    host_ports_by_role: dict[str, set[str]] = {"port_a": set(), "port_b": set()}
    for host in hosts:
        require(bool(HOSTNAME.fullmatch(str(host.get("name", "")))), "invalid host name")
        pc = host.get("port_channel")
        require(isinstance(pc, int) and 1 <= pc <= 4096 and pc not in host_pcs, "invalid or duplicate host port-channel")
        host_pcs.add(pc)
        for key in ("port_a", "port_b"):
            interface_number(host[key])
            require(host[key] not in host_ports_by_role[key], f"AI/OpenShift interface {host[key]} is reused")
            host_ports_by_role[key].add(host[key])
        host_vlans = validate_vlan_expression(host["allowed_vlans"], allowed_all, f"host {host['name']}")
        native_vlan = host.get("native_vlan")
        if native_vlan is not None:
            require(isinstance(native_vlan, int), f"host {host['name']} native VLAN must be an integer")
            require(native_vlan in host_vlans, f"host {host['name']} native VLAN must be included in its allowed VLANs")

    cx7 = next(host for host in hosts if host.get("attachment_role") == "c845a-cx7")
    require(
        cx7.get("port_channel") == 101
        and cx7.get("port_a") == "Ethernet1/1"
        and cx7.get("port_b") == "Ethernet1/1",
        "C845A CX-7 must use Po101/vPC101 with Ethernet1/1 on each AI/GPU switch",
    )
    require(
        cx7.get("native_vlan") == 3056
        and cx7.get("allowed_vlans") == "3056"
        and cx7.get("mtu") == 9216
        and cx7.get("pfc") is True,
        "C845A CX-7 must use native/allowed VLAN 3056 with MTU 9216 and PFC",
    )

    x710 = next(host for host in hosts if host.get("attachment_role") == "c845a-x710-ocp")
    require(
        x710.get("port_channel") == 102
        and x710.get("port_a") == "Ethernet1/33"
        and x710.get("port_b") == "Ethernet1/33",
        "C845A X710 reference design must use Po102/vPC102 with Ethernet1/33 on each AI/GPU switch",
    )
    require(
        x710.get("native_vlan") == 1082
        and validate_vlan_expression(
            x710.get("allowed_vlans", ""), allowed_all, "C845A X710 OpenShift path"
        ) in ({1082}, {1082, 3057})
        and x710.get("mtu") == 1500
        and x710.get("pfc") is False,
        "C845A X710 must use native VLAN 1082, optional provisioning VLAN 3057 only, MTU 1500, and no PFC",
    )

    return data


def render(data: dict[str, Any], output_dir: Path) -> list[Path]:
    environment = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    jobs = (
        ("vast-storage-9332d.nxos.j2", "storage_pair", data["storage_pair"]),
        ("ai-gpu-ocp-9332d.nxos.j2", "ai_gpu_ocp_pair", data["ai_gpu_ocp_pair"]),
    )
    for template_name, pair_name, pair in jobs:
        template = environment.get_template(template_name)
        for switch in pair["switches"]:
            text = template.render(data=data, pair=pair, switch=switch)
            require("9336" not in text.upper(), "rendered configuration unexpectedly references a 9336")
            require("shutdown" in text, "rendered configuration does not preserve shutdown-by-default")
            destination = output_dir / f"{pair_name}-{switch['role']}.cfg"
            destination.write_text(text, encoding="utf-8")
            destination.chmod(0o600)
            outputs.append(destination)
    return outputs


def main() -> int:
    args = parse_args()
    try:
        values = yaml.safe_load(args.values.read_text(encoding="utf-8"))
        require(isinstance(values, dict), "values file must contain a YAML mapping")
        data = validate(values)
        outputs = render(data, args.output_dir)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    for output in outputs:
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        print(f"RENDERED: {output} sha256={digest}")
    print("PASS: four shutdown-by-default candidate configurations rendered; no devices were contacted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
