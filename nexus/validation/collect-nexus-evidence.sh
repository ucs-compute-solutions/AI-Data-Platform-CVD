#!/usr/bin/env bash

set -euo pipefail

readonly DEFAULT_USERNAME="admin"

usage() {
  cat <<'EOF'
Collect a read-only Cisco Nexus evidence snapshot.

Usage:
  collect-nexus-evidence.sh --inventory FILE [--username USER] [--output-dir DIR]

Inventory:
  Copy switches.example.csv to switches.site.csv and replace each example
  address. The CSV columns are role,address. Credentials do not belong in it.

Authentication:
  OpenSSH prompts interactively for each switch. Passwords are not read,
  stored, logged, or passed through command-line arguments by this script.

Safety:
  - SSH host-key checking remains enabled.
  - Only the embedded allowlist of operational "show" commands is sent.
  - The script never enters configuration mode or saves configuration.
  - Output defaults to a mode-0700 temporary directory outside Git.
EOF
}

username="$DEFAULT_USERNAME"
output_dir=""
inventory_file=""

while (($#)); do
  case "$1" in
    --username)
      [[ $# -ge 2 ]] || { echo "ERROR: --username requires a value" >&2; exit 2; }
      username="$2"
      shift 2
      ;;
    --inventory)
      [[ $# -ge 2 ]] || { echo "ERROR: --inventory requires a value" >&2; exit 2; }
      inventory_file="$2"
      shift 2
      ;;
    --output-dir)
      [[ $# -ge 2 ]] || { echo "ERROR: --output-dir requires a value" >&2; exit 2; }
      output_dir="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ -n "$inventory_file" ]] || {
  echo "ERROR: --inventory is required" >&2
  usage >&2
  exit 2
}

if [[ ! "$username" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "ERROR: username contains unsupported characters" >&2
  exit 2
fi

umask 077

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(git -C "$script_dir" rev-parse --show-toplevel 2>/dev/null || true)"

[[ -f "$inventory_file" && ! -L "$inventory_file" && -r "$inventory_file" ]] || {
  echo "ERROR: inventory must be a readable regular, non-symlink file: $inventory_file" >&2
  exit 2
}

declare -a switches=()
declare -A seen_roles=()
while IFS=',' read -r role address extra; do
  role="${role//$'\r'/}"
  address="${address//$'\r'/}"
  [[ -z "$role" || "$role" == \#* || "$role" == "role" ]] && continue
  [[ -z "${extra:-}" ]] || {
    echo "ERROR: inventory rows must contain exactly role,address" >&2
    exit 2
  }
  [[ "$role" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
    echo "ERROR: invalid switch role in inventory: $role" >&2
    exit 2
  }
  [[ "$address" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || {
    echo "ERROR: invalid switch address for role $role" >&2
    exit 2
  }
  [[ -z "${seen_roles[$role]:-}" ]] || {
    echo "ERROR: duplicate switch role in inventory: $role" >&2
    exit 2
  }
  seen_roles[$role]=1
  switches+=("$role|$address")
done < "$inventory_file"

((${#switches[@]} > 0)) || {
  echo "ERROR: inventory contains no switch targets" >&2
  exit 2
}

if [[ -z "$output_dir" ]]; then
  output_dir="$(mktemp -d /tmp/cvd-nexus-evidence.XXXXXX)"
else
  mkdir -p -- "$output_dir"
  output_dir="$(cd -- "$output_dir" && pwd -P)"
fi

if [[ -n "$repo_root" && ( "$output_dir" == "$repo_root" || "$output_dir" == "$repo_root/"* ) ]]; then
  echo "ERROR: raw switch evidence must remain outside the Git repository" >&2
  exit 2
fi

chmod 700 "$output_dir"

echo "Read-only Cisco Nexus evidence collection"
echo "SSH username: $username"
echo "Output directory: $output_dir"
echo "SSH will prompt for the switch password; input will remain hidden."
echo

for switch in "${switches[@]}"; do
  IFS='|' read -r role address <<<"$switch"
  output_file="$output_dir/${role}.txt"

  echo "================================================================"
  echo "Target: $role ($address)"
  echo "Commands: allowlisted operational show commands only"
  echo "================================================================"

  if ssh \
      -tt \
      -o BatchMode=no \
      -o StrictHostKeyChecking=ask \
      -o ConnectTimeout=15 \
      -o ServerAliveInterval=15 \
      -o ServerAliveCountMax=2 \
      "$username@$address" \
      > >(tee "$output_file") 2>&1 <<'NXOS_COMMANDS'
terminal length 0
show version
show inventory
show feature
show clock
show ntp peers
show vpc brief
show vpc consistency-parameters global
show port-channel summary
show lacp neighbor
show running-config interface ethernet1/1
show running-config interface ethernet1/24
show running-config interface ethernet1/25
show running-config interface ethernet1/33
show running-config interface port-channel100
show running-config interface port-channel101
show running-config interface port-channel102
show interface status
show interface description
show interface trunk
show vlan brief
show lldp neighbors detail
show spanning-tree inconsistentports
show interface counters errors
show interface priority-flow-control
show policy-map system type network-qos
show logging last 50
exit
NXOS_COMMANDS
  then
    chmod 600 "$output_file"
    echo "PASS: collected $role"
  else
    status=$?
    chmod 600 "$output_file" 2>/dev/null || true
    echo "ERROR: collection failed for $role with status $status" >&2
    echo "No switch configuration changes were attempted." >&2
    exit "$status"
  fi
done

echo
echo "PASS: collected all four read-only switch snapshots."
echo "Raw evidence remains outside Git: $output_dir"
echo "Do not copy these files into the repository; they may contain site details."
