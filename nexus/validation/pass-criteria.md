# Cisco Nexus acceptance criteria

Run the checks on both members of each Cisco Nexus 9332D-GX2B pair. Retain only sanitized command output in the CVD evidence package.

| Check | Command | Pass condition |
|---|---|---|
| Platform | `show version` | Correct C9332D-GX2B platform; both members of a pair run the same approved NX-OS release. |
| vPC health | `show vpc brief` | Peer adjacency is formed, keepalive is alive, and configuration, per-VLAN, and Type-2 consistency show `success`. |
| Port channels | `show port-channel summary` | Peer, inter-fabric, and approved host port channels show `SU`; expected members show `P`. |
| LACP | `show lacp neighbor` | Every expected member has the correct partner. |
| STP | `show spanning-tree inconsistentports` | No inconsistent ports. |
| VLAN pruning | `show interface trunk` | Only required VLANs are forwarded. VLAN 1 is not carried unless the site design explicitly requires and documents it. |
| MTU and PFC | `show policy-map system type network-qos` and `show interface priority-flow-control` | MTU 9216 and PFC CoS 3 are operational on the approved lossless paths. |
| Port roles | `show interface status` and `show interface description` | Storage pair uses Ethernet1/1/1-Ethernet1/8/2 for external/northbound and Ethernet1/9/1-Ethernet1/16/2 for storage/internal connections. |
| Inter-fabric mapping | `show port-channel summary`, `show interface description`, and `show lldp neighbors` | Po100/vPC100 is up with Ethernet1/24-25 as members. Storage A maps to AI/GPU A, and Storage B maps to AI/GPU B. |
| C845A CX-7 data path | `show running-config interface port-channel101`, `show interface port-channel101 trunk`, and `show vpc brief` | Po101/vPC101 is up on Ethernet1/1 on both AI/GPU switches; native and allowed VLAN are 3056; MTU 9216 and the approved PFC/RoCE policy are present. |
| C845A X710 OpenShift path | `show running-config interface port-channel102`, `show interface port-channel102 trunk`, and `show vpc brief` | After the site confirms the reference design, Po102/vPC102 is up on Ethernet1/33 on both AI/GPU switches; VLAN 1082 is native, VLAN 3057 is present only if provisioning requires it, MTU is 1500, and PFC is not enabled. |
| Errors | `show interface counters errors` | No new CRC, discard, or error increments during the validation test. |

The Po102/Ethernet1/33 row is a target-design acceptance check, not evidence that this exact X710 switch mapping was present in the archived validation capture. If the approved site port map uses different X710 interfaces or a different host attachment method, update `values.site.yaml` and the acceptance record together.

## Counter procedure

1. Capture `show interface counters errors` before traffic testing.
2. If the maintenance plan permits it, clear counters with the site-approved NX-OS command, such as `clear counters interface all`.
3. Run the storage and OpenShift traffic tests.
4. Capture the counters again and compare them with the baseline. Historical nonzero values are not a failure by themselves; unexpected increases are.

The provided `collect-nexus-evidence.sh` script is read-only and does not clear counters.
