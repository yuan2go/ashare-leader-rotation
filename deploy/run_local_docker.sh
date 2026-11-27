#!/usr/bin/env bash
set -euo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
action=Up
mode=ReadOnly
while (($#)); do
    case "$1" in
        -Action|--action) [[ $# -ge 2 ]] || exit 2; action=$2; shift 2 ;;
        -Mode|--mode) [[ $# -ge 2 ]] || exit 2; mode=$2; shift 2 ;;
        Up|Start|Down|Rebuild|Status|Logs|Smoke) action=$1; shift ;;
        *) printf '%s\n' 'Usage: deploy/run_local_docker.sh -Action Up|Start|Down|Rebuild|Status|Logs|Smoke -Mode ReadOnly|Interactive' >&2; exit 2 ;;
    esac
done
exec "${PYTHON_BIN:-python3.11}" -B -X utf8 "$repo/deploy/local_docker.py" "$action" --mode "$mode"
