from __future__ import annotations

import argparse
import asyncio
import signal
import sys

from .agent import AgentRunner, CodexAppServerClient
from .config import build_config
from .logging import configure_logging
from .orchestrator import Orchestrator
from .server import StatusServer
from .tracker import build_tracker
from .workflow import load_workflow, select_workflow_path
from .workspace import WorkspaceManager


def build_runner(config, workspace_manager):
    return AgentRunner(config, workspace_manager, CodexAppServerClient(config, workspace_manager))


async def run_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="symphony")
    parser.add_argument("workflow", nargs="?", help="path to WORKFLOW.md")
    parser.add_argument("--port", type=int, help="enable HTTP status server on this port")
    args = parser.parse_args(argv)

    logger = configure_logging()
    workflow_path = select_workflow_path(args.workflow)
    workflow = load_workflow(workflow_path)
    config = build_config(workflow)
    if args.port is not None:
        config.server.port = args.port
    orchestrator = Orchestrator(
        workflow,
        config,
        tracker=build_tracker(config),
        runner_factory=build_runner,
        logger=logger,
    )
    server = None
    if config.server.port is not None and config.server.port >= 0:
        server = StatusServer(orchestrator, config.server.host, config.server.port)
        await server.start()
        logger.info("status_server started host=%s port=%s", server.server_address[0], server.server_address[1])

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    task = asyncio.create_task(orchestrator.start())
    waiter = asyncio.create_task(stop_event.wait())
    done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    if waiter in done:
        orchestrator.stop()
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    if server:
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
