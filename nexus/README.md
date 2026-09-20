# Cisco Nexus CVD configuration package

This directory provides a small, offline starting point for the two Cisco Nexus 9332D-GX2B pairs in the AIDP design:

- **VAST storage pair:** 12 Cisco C225 M8 EBox nodes. Ethernet1/1/1-Ethernet1/8/2 are external/northbound ports; Ethernet1/9/1-Ethernet1/16/2 are storage/internal ports.
- **AI/GPU/OpenShift pair:** three Cisco C225 M8 OpenShift nodes and one Cisco C845A GPU node, with separate X710 OpenShift and CX-7 VAST/RoCE attachments and the production northbound connection.
- **Inter-fabric path:** Storage A Ethernet1/24-25 connects to AI/GPU A Ethernet1/24-25, and Storage B Ethernet1/24-25 connects to AI/GPU B Ethernet1/24-25. The four 400-Gb links form Po100/vPC100. No third switch pair is part of this CVD.

The renderer creates four candidate configuration files with data-plane interfaces set to `shutdown`. It never connects to a switch and there is no apply script.

## Validated mapping and reference-design inputs

The example separates facts observed in the validation environment from the repeatable target design that a deployment team must confirm against its final cabling schedule.

| Path | Reference mapping | VLAN and transport | Status in this package |
|---|---|---|---|
| Storage A to AI/GPU A | Ethernet1/24-25 to Ethernet1/24-25; Po100/vPC100 | Candidate trunk permits VLANs 1080-1082 and 3056; MTU 9216 | Physical members and Po100 were observed up. Confirm the approved site VLAN pruning before use. |
| Storage B to AI/GPU B | Ethernet1/24-25 to Ethernet1/24-25; Po100/vPC100 | Candidate trunk permits VLANs 1080-1082 and 3056; MTU 9216 | Physical members and Po100 were observed up. Confirm the approved site VLAN pruning before use. |
| C845A CX-7 VAST/RoCE | AI/GPU A/B Ethernet1/1; Po101/vPC101; host `bond1` | Native and allowed VLAN 3056; 2 x 200 Gb; MTU 9216; PFC/RoCE policy | Validated path. |
| C845A X710 OpenShift | AI/GPU A/B Ethernet1/33; Po102/vPC102; host `bond0` and `br-ex` | Native VLAN 1082; VLAN 3057 only when the installation workflow uses provisioning; MTU 1500; no PFC | CVD target. Confirm both switch endpoints, transceivers, Po102/vPC102, and the provisioning requirement at the site before applying. |

VLAN 1080 is the VAST management service network and VLAN 1081 is the VAST in-band service network. They can traverse approved fabric trunks where those VAST services require them, but they are not the active C845A X710 machine-network attachment. The C845A OpenShift node IP and default route use VLAN 1082 through `br-ex`; VAST/RoCE data uses the separate CX-7 VLAN-3056 path.

The captured Po100 configuration permitted VLANs 1080-1082 and 3056. Some site designs may intentionally prune Po100 to VLAN 3056. Use one approved list consistently on both ends; do not copy a captured list without checking the final traffic matrix.

## Files

```text
nexus/
├── README.md
├── .gitignore
├── requirements.txt
├── values.example.yaml
├── render.py
├── templates/
│   ├── vast-storage-9332d.nxos.j2
│   └── ai-gpu-ocp-9332d.nxos.j2
└── validation/
    ├── collect-nexus-evidence.sh
    └── pass-criteria.md
```

## Render and review

```bash
cd AI-Data-Platform-CVD/nexus
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp values.example.yaml values.site.yaml
# Review and edit values.site.yaml for the target site.
.venv/bin/python render.py --values values.site.yaml --output-dir rendered
```

The renderer stops if it finds unresolved markers, secret-like keys, a 9336/third fabric, an unexpected VLAN role, a changed storage port range, or a configuration that is not shutdown by default.

Before using any candidate configuration:

1. Compare it with the approved physical port map and current running configuration.
2. Confirm whether the site uses the X710 provisioning VLAN 3057 after installation; remove it from the C845A X710 trunk if it is not required.
3. Have a second network engineer review the diff, VLAN pruning, vPC IDs, peer links, inter-fabric links, host links, MTU, and PFC.
4. Use the normal maintenance and change-control process. Apply changes manually, one switch and one link at a time; remove `shutdown` only after the preceding checks pass.
5. Validate the result with `validation/collect-nexus-evidence.sh` and `validation/pass-criteria.md`.

Copy and review the switch inventory before collecting evidence:

```bash
cp validation/switches.example.csv validation/switches.site.csv
${EDITOR:-vi} validation/switches.site.csv
./validation/collect-nexus-evidence.sh \
  --inventory validation/switches.site.csv
```

If a change does not validate, shut the newly enabled link, restore the approved checkpoint or saved configuration using the site's standard recovery procedure, and verify that the unaffected peer remains healthy. Do not store switch credentials, raw running configurations, or unsanitized evidence in Git.
