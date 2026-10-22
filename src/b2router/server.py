"""Process entrypoint.

Order matters: validate settings, take the single-writer lock, parse the
configuration, authorize every account and build the cache, and only then
start accepting HTTP traffic. A partially built pool never serves a request.
"""

from __future__ import annotations

import argparse
import signal
import sys
from dataclasses import replace

from b2router.core.config_reader import ConfigReader
from b2router.lockfile import InstanceLock, LockError
from b2router.logging_setup import configure_logging, log_event
from b2router.services.account import AccountClient, AccountCredentials
from b2router.services.audit import AuditLog
from b2router.services.pool import AccountPool, PoolStartupError
from b2router.services.uploads import UploadManager, cleanup_temp_dir
from b2router.settings import Settings, SettingsError
from b2router.utils.result import Err
from b2router.web import create_app


class ConfigStartupError(Exception):
    def __init__(self, issues: list[str]) -> None:
        self.issues = issues
        super().__init__("; ".join(issues))


def build_pool(settings: Settings) -> AccountPool:
    result = ConfigReader(settings.accounts_path).read()
    if isinstance(result, Err):
        raise ConfigStartupError([result.error.message])
    config = result.value

    pool = AccountPool(max_files_per_account=config.max_files_per_account)
    accounts = []
    for spec in config.accounts:
        credentials = AccountCredentials(
            name=spec.name,
            application_key_id=spec.application_key_id,
            application_key=spec.application_key,
            connect_timeout=settings.connect_timeout,
            read_timeout=settings.read_timeout,
        )
        accounts.append((AccountClient(credentials), spec.capacity_bytes))
    pool.load_accounts(accounts)
    return pool


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="b2router", description="B2 routing gateway")
    parser.add_argument("--host", help="Bind address (overrides B2ROUTER_HOST)")
    parser.add_argument("--port", type=int, help="Bind port (overrides B2ROUTER_PORT)")
    parser.add_argument("--accounts", help="accounts.yaml path")
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_env()
    except SettingsError as exc:
        configure_logging("INFO", "pretty")
        for issue in exc.issues:
            log_event("config.invalid", level=40, issue=issue)
        return 2

    if args.host:
        settings = replace(settings, host=args.host)
    if args.port:
        settings = replace(settings, port=args.port)
    if args.accounts:
        settings = replace(settings, accounts_path=args.accounts)

    configure_logging(settings.log_level, settings.log_format)

    lock = InstanceLock(settings.lock_file)
    if settings.acquire_lock:
        try:
            lock.acquire()
        except LockError as exc:
            log_event("startup.lock_failed", level=40, error=str(exc))
            return 3
        except OSError as exc:
            log_event("startup.lock_failed", level=40, error=str(exc))
            return 3

    manager: UploadManager | None = None
    try:
        try:
            pool = build_pool(settings)
        except ConfigStartupError as exc:
            for issue in exc.issues:
                log_event("startup.config_failed", level=40, issue=issue)
            return 4
        except PoolStartupError as exc:
            for issue in exc.issues:
                log_event("startup.account_failed", level=40, issue=issue)
            return 4

        audit = AuditLog(maxlen=max(settings.job_history_max, 1000))
        manager = UploadManager(pool, audit, settings)
        app = create_app(settings, pool=pool, manager=manager, audit=audit)
        app.extensions["b2router"].lock = lock

        stale = cleanup_temp_dir(settings.temp_dir)
        log_event(
            "startup.ready",
            version=settings.version,
            accounts=len(pool),
            files=pool.stats()["files"],
            stale_spool_files=stale,
            host=settings.host,
            port=settings.port,
            workers=settings.upload_workers,
        )

        # Werkzeug does not translate SIGTERM into a controlled exit. Without
        # this handler Docker/systemd would kill the process without draining
        # accepted uploads or releasing the writer lock in our finally block.
        def _on_sigterm(_signum: int, _frame: object) -> None:
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, _on_sigterm)
        app.run(
            host=settings.host,
            port=settings.port,
            threaded=True,
            use_reloader=False,
            debug=False,
        )
        return 0
    except KeyboardInterrupt:  # pragma: no cover
        return 0
    finally:
        # Drain accepted uploads and deletes before releasing the writer lock.
        # A second process must never start while this one can still mutate B2.
        try:
            if manager is not None:
                manager.shutdown()
        finally:
            lock.release()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
