"""BitBotPro worker process (Spec 012, D-Q4). App wiring only.

    APP_MODE=worker python worker.py          # docker: same image, command ["python", "worker.py"]
    python worker.py --healthcheck            # exit 0 while the heartbeat is fresh

Runs deletion jobs, binding reconciliation, deliveries, Telegram webhook sync,
Zendesk maintenance, knowledge ingestion and the Discord gateway (one connection
per enabled Enterprise application, D-Q14). Never started by the API process.
SIGTERM/SIGINT stop it cleanly (the gateway closes with 1000; leases expire).
"""
import asyncio
import logging
import os
import signal
import sys

import log_redaction


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    from api.production import ProductionConfig, ProductionConfigError, run_worker, worker_healthy
    if argv == ["--healthcheck"]:
        return 0 if worker_healthy() else 1
    if argv:
        print("usage: worker.py [--healthcheck]", file=sys.stderr)
        return 64
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log_redaction.install()
    try:
        config = ProductionConfig.from_environment(os.environ)
    except ProductionConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 78

    async def run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run_worker(config, stop=stop)

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
