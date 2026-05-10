from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import signal
import sys

from .logging import configure_logging


async def run_async(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "harness":
        argv = argv[1:]

    from .harness import run_harness_server

    parser = argparse.ArgumentParser(prog="symphony harness")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", help="directory for harness sqlite database and task artifacts")
    parser.add_argument("--project-root", default=".")
    args = parser.parse_args(argv)

    logger = configure_logging()
    server = await run_harness_server(
        project_root=Path(args.project_root).resolve(),
        host=args.host,
        port=args.port,
        data_dir=args.data_dir,
    )
    logger.info("harness_server started url=http://%s:%s", server.server_address[0], server.server_address[1])
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass
    await stop_event.wait()
    await server.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(run_async(argv))
    except Exception as exc:
        logger = configure_logging()
        logger.error("startup failed reason=%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
