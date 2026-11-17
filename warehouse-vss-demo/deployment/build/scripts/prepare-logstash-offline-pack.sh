#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  prepare-logstash-offline-pack.sh --output DIRECTORY [--execute]

Without --execute, print the pinned staging command only. With --execute, run
the immutable Logstash 9.3.3 image on the local Docker host, install
logstash-codec-protobuf 1.3.0 from RubyGems, and create Elastic's supported
offline plug-in pack. The command does not push an image or change OpenShift.
EOF
}

output_dir=""
execute=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output)
      output_dir=${2:?missing value for --output}
      shift 2
      ;;
    --execute)
      execute=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$output_dir" ]]; then
  usage >&2
  exit 2
fi

base_image="docker.elastic.co/logstash/logstash@sha256:a7ad817ec23e2f9c41f8fcd09bcce3c87710890f93612d4fd51de45074d7af2e"
pack_name="logstash-offline-plugins-9.3.3-protobuf-1.3.0.zip"

echo "Base image: $base_image"
echo "Plug-in: logstash-codec-protobuf 1.3.0"
echo "Output: $output_dir/$pack_name"

if [[ "$execute" != true ]]; then
  echo "PLAN ONLY: add --execute to pull/run the pinned staging image and create the pack."
  exit 0
fi

command -v docker >/dev/null || {
  echo "docker is required" >&2
  exit 1
}
mkdir -p -- "$output_dir"
output_dir=$(cd -- "$output_dir" && pwd)
if [[ -e "$output_dir/$pack_name" ]]; then
  echo "Refusing to overwrite existing pack: $output_dir/$pack_name" >&2
  exit 1
fi

docker run --rm --platform linux/amd64 --user 0 \
  --volume "$output_dir:/output" \
  "$base_image" \
  bash -lc "/usr/share/logstash/bin/logstash-plugin install --version 1.3.0 logstash-codec-protobuf && /usr/share/logstash/bin/logstash-plugin prepare-offline-pack --output /output/$pack_name --overwrite logstash-codec-protobuf"

sha256sum "$output_dir/$pack_name"
echo "NEXT: record this SHA-256 in derived-images.lock.json before building the derived Logstash image."
