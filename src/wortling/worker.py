import logging
import signal
import threading

from wortling.config import Settings
from wortling.db import Database


def heartbeat(settings: Settings) -> None:
    path = settings.data_dir / "worker-heartbeat"
    path.touch(mode=0o600, exist_ok=True)


def run(settings: Settings, *, once: bool = False) -> None:
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    db = Database(settings.data_dir)
    db.initialize(settings)
    from wortling.pipeline.engine import Pipeline
    from wortling.scheduler import tick

    pipeline = Pipeline(settings, db)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    logging.info("Wortling worker ready; writes remain controlled per learner")
    while not stop.is_set():
        heartbeat(settings)
        tick(settings, db)
        work = pipeline.run_once()
        heartbeat(settings)
        if once:
            break
        if not work:
            stop.wait(settings.worker_poll_seconds)
