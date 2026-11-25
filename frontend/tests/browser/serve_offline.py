"""Serve the signed local ledger for a read-only browser smoke test."""
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from starlette.responses import JSONResponse

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
PORT = int(os.environ.get("PHASE7B_SMOKE_PORT", "18742"))
COUNTER = os.environ.get("PHASE7B_NETWORK_AUDIT")
loopback = {"127.0.0.1", "::1", "localhost", None}
attempts = 0


def record():
    if COUNTER:
        Path(COUNTER).write_text(json.dumps({"external_attempts": attempts, "market_http_requests": 0}), encoding="utf-8")


def outbound(event, args):
    global attempts
    if event in ("socket.connect", "socket.sendto"):
        address = args[1]
        host = address[0] if isinstance(address, tuple) else None
    elif event == "socket.getaddrinfo":
        host = args[0]
    elif event == "http.client.connect":
        host = args[1]
    elif event == "urllib.Request":
        host = urlsplit(args[0]).hostname
    else:
        return
    if host not in loopback:
        attempts += 1
        record()
        raise RuntimeError("Phase 7B browser smoke prohibits external network requests")


if __name__ == "__main__":
    from core.integrity import require_runtime_guard

    require_runtime_guard()
    sys.addaudithook(outbound)
    record()
    from backend.app.main import app

    @app.middleware("http")
    async def readonly(request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            return JSONResponse({"code": "read_only_smoke", "message": "冒烟服务仅提供只读访问"}, status_code=405)
        return await call_next(request)

    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
