#!/usr/bin/env bash
set -euo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
action=Up
if [[ ${1:-} == -Action || ${1:-} == --action ]]; then
    [[ $# == 2 ]] || { printf '%s\n' 'Usage: deploy/run_local_docker.sh -Action Up|Down|Rebuild|Status|Logs|Smoke' >&2; exit 2; }
    action=$2
elif [[ $# == 1 ]]; then
    action=$1
elif [[ $# != 0 ]]; then
    printf '%s\n' 'Usage: deploy/run_local_docker.sh -Action Up|Down|Rebuild|Status|Logs|Smoke' >&2
    exit 2
fi
exec "${PYTHON_BIN:-python3.11}" -B -X utf8 "$repo/deploy/local_docker.py" "$action"
