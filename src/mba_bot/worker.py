"""The trading worker: `python -m mba_bot.worker` (docs/safety/process_split.md). Runs the scheduler (trading rounds, recovery, stop-limit monitor) and NOTHING that serves HTTP, so the API process can be released and
restarted without touching it. Not used unless the owner starts it: the default is still the single process (`PROCESS_ROLE` unset = `all`).

    python -m mba_bot.worker            run
    python -m mba_bot.worker --check    do every start-up check (role, WAL, lock, imports) and exit without running a round; 0 = would start, 1 = would refuse

On start it takes the SAME scheduler lock as the single process, so it cannot run next to one. It reconciles every live trade with the exchange before its first round (the scheduler's own start-up step), exactly as
the single process does. It handles SIGTERM by stopping its loops cleanly; a round already running is cancelled, and protective stops stay on the exchange."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from . import daily_limit, process_roles
from .config import load_settings, warn_about_legacy_futures_testnet_env
from .db import init_db
from .instance_lock import AlreadyRunning, acquire_single_instance_lock
from .main import configure_logging
from .scheduler import run_forever

logger = logging.getLogger(__name__)


def startup_checks(settings, *, take_lock: bool = True):
    """Every refusal in one place. Returns the lock handle (keep it referenced), or None when `take_lock` is False. Raises SystemExit with a plain message."""
    try:
        process_roles.refuse_other_role(settings, process_roles.ROLE_WORKER)
        process_roles.require_wal_setting(settings)
    except process_roles.RoleError as exc:
        raise SystemExit(str(exc)) from None
    if not settings.encryption_key:
        raise SystemExit("ENCRYPTION_KEY is not set - generate one with `python -m mba_bot.crypto_utils`")
    handle = None
    if take_lock:
        try:
            handle = acquire_single_instance_lock(settings.lock_file)
        except AlreadyRunning as exc:
            raise SystemExit(str(exc)) from None
    failed = process_roles.preload()
    if failed:
        raise SystemExit("modules failed to import: " + ", ".join(failed))
    return handle


async def amain(settings) -> None:
    task = asyncio.ensure_future(run_forever(settings, billing=False))  # the billing jobs run in the API process
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, task.cancel)
        except NotImplementedError:  # pragma: no cover - not on Linux
            pass
    try:
        await task
    except asyncio.CancelledError:
        logger.info("worker stopping on request")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    check_only = "--check" in argv
    settings = load_settings()
    configure_logging(settings)
    warn_about_legacy_futures_testnet_env(logging.getLogger(__name__))
    try:
        handle = startup_checks(settings, take_lock=not check_only)  # noqa: F841 - held for the life of the process
        if check_only:
            # the lock is tested without keeping it: a held lock means a scheduler is already running
            try:
                acquire_single_instance_lock(settings.lock_file).close()
            except AlreadyRunning as exc:
                raise SystemExit(str(exc)) from None
            process_roles.require_wal_on_disk(settings)
            print("worker checks passed: role, WAL, lock, imports")
            return 0
        init_db(settings.database_url, wal=True, busy_timeout_ms=settings.sqlite_busy_timeout_ms)
        process_roles.require_wal_on_disk(settings)
        daily_limit.run_startup_default()
        asyncio.run(amain(settings))
        return 0
    except process_roles.RoleError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except SystemExit as exc:
        if exc.code not in (None, 0):
            print(str(exc.code), file=sys.stderr)
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
