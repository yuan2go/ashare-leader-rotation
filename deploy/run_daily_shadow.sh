#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: deploy/run_daily_shadow.sh [DailyAll|Collect|Ingest|VerifyOffline|Status|Healthcheck|ServeApi|BuildFrontend]
       [--date YYYY-MM-DD] [--port 8000] [--host 127.0.0.1] [--python BIN] [--notify-on-pending]
EOF
}

action=DailyAll
if [[ $# -gt 0 && $1 != -* ]]; then
    action=$1
    shift
fi
case "$action" in
    DailyAll|Collect|Ingest|VerifyOffline|Status|Healthcheck|ServeApi|BuildFrontend) ;;
    *) usage >&2; exit 2 ;;
esac

day=
host=127.0.0.1
port=8000
python_bin=${PYTHON_BIN:-python3.11}
notify_pending=()
while (($#)); do
    case "$1" in
        --date|--host|--port|--python)
            if [[ $# -lt 2 || -z $2 || $2 == --* ]]; then
                usage >&2
                exit 2
            fi
            case "$1" in
                --date) day=$2 ;;
                --host) host=$2 ;;
                --port) port=$2 ;;
                --python) python_bin=$2 ;;
            esac
            shift 2 ;;
        --notify-on-pending) notify_pending=(--notify-on-pending); shift ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

python_bin=$(command -v -- "$python_bin")
if [[ $python_bin != /* && -f $python_bin ]]; then
    python_bin="$(cd -- "$(dirname -- "$python_bin")" && pwd)/$(basename -- "$python_bin")"
fi
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$repo"
export TZ=Asia/Shanghai PYTHONUTF8=1 PYTHONIOENCODING=utf-8
"$python_bin" -B -X utf8 -c 'from core.integrity import require_runtime_guard; require_runtime_guard()'

case "$action" in
    BuildFrontend)
        cd frontend
        npm run typecheck
        exec npm run build ;;
    ServeApi)
        proxy_args=(--no-proxy-headers)
        if [[ -n ${PHASE7A_UVICORN_FORWARDED_ALLOW_IPS:-} ]]; then
            proxy_args=(--proxy-headers --forwarded-allow-ips "$PHASE7A_UVICORN_FORWARDED_ALLOW_IPS")
        fi
        exec "$python_bin" -B -X utf8 -m uvicorn backend.app.main:app \
            --host "$host" --port "$port" --workers 1 "${proxy_args[@]}" ;;
    Status|Healthcheck)
        flag=--status
        if [[ $action == Healthcheck ]]; then flag=--healthcheck; fi
        exec "$python_bin" -B -X utf8 scripts/run_phase7a_shadow.py "$flag" ;;
    Ingest|VerifyOffline)
        exec "$python_bin" -B -X utf8 -m backend.app.services.ops_service --action "$action" ;;
    DailyAll|Collect)
        if [[ -z $day ]]; then
            day=$("$python_bin" -B -X utf8 -c 'from datetime import datetime; from zoneinfo import ZoneInfo; print(datetime.now(ZoneInfo("Asia/Shanghai")).date())')
        fi
        code=0
        "$python_bin" -B -X utf8 -m backend.app.services.ops_service --action "$action" --date "$day" || code=$?
        if [[ -n ${SHADOW_NOTIFY_WEBHOOK_URL:-} || -n ${SHADOW_SMTP_HOST:-} ]]; then
            dry_run=()
            if [[ ${SHADOW_NOTIFY_DRY_RUN:-0} == 1 ]]; then dry_run=(--dry-run); fi
            "$python_bin" -B -X utf8 scripts/notify_shadow_event.py \
                --status-dir "${PHASE7A_DIR:-$repo/reports/phase7a_service_shadow}" \
                --exit-code "$code" --action "$action" --date "$day" \
                "${notify_pending[@]}" "${dry_run[@]}" ||
                printf 'Shadow notification failed (exit=%s)\n' "$?" >&2
        fi
        exit "$code" ;;
esac
