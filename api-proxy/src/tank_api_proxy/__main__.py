"""Module entrypoint: ``python -m tank_api_proxy`` boots the gRPC server."""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from .server import serve


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_run())


async def _run() -> None:
    port = int(os.environ.get("EXT_PROC_PORT", "9000"))
    server = await serve(port)
    stop = asyncio.Event()

    def _shutdown(*_: object) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            # Windows in dev — signal handlers aren't supported on the
            # default loop. The server will exit on KeyboardInterrupt.
            pass

    await stop.wait()
    await server.stop(grace=5)


if __name__ == "__main__":
    main()
