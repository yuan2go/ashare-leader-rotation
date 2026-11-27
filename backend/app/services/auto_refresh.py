"""Schedule one isolated refresh subprocess at a time for the local workbench."""

import asyncio
from contextlib import asynccontextmanager, suppress
import logging
import os
import sys

from ..config import ROOT

INTERVAL_SECONDS = 600
log = logging.getLogger(__name__)


async def run_refresh_loop():
    loop = asyncio.get_running_loop()
    while True:
        next_check = loop.time() + INTERVAL_SECONDS
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-B", "-X", "utf8", "-m", "scripts.poll_local_shadow", cwd=ROOT,
                stdin=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            log.warning("Failed to start local market refresh (%s)", type(exc).__name__)
        else:
            try:
                code = await process.wait()
            except asyncio.CancelledError:
                with suppress(ProcessLookupError):
                    process.terminate()
                await process.wait()
                raise
            if code:
                log.warning("Local market refresh exited with code %s", code)
        await asyncio.sleep(max(0, next_check - loop.time()))


@asynccontextmanager
async def lifespan(app):
    task = None
    if os.environ.get("PHASE11_AUTO_REFRESH") == "1" and not app.state.settings.api_read_only:
        task = asyncio.create_task(run_refresh_loop())
    try:
        yield
    finally:
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
