"""Foreground lifecycle wrapper for Fleet's local managed-projection service."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
import signal
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._paths import is_concrete_path

logger = logging.getLogger(__name__)
_MAX_SHUTDOWN_TIMEOUT_SECONDS = 60


@dataclass(frozen=True, slots=True)
class ManagedServiceConfig:
    socket_path: Path
    database_path: Path
    allowed_uid: int
    shutdown_timeout_seconds: int
    socket_gid: int | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.socket_path, "socket_path"),
            (self.database_path, "database_path"),
        ):
            if not is_concrete_path(value) or not value.is_absolute():
                raise ValueError(f"{name} must be an absolute Path")
        if (
            isinstance(self.allowed_uid, bool)
            or not isinstance(self.allowed_uid, int)
            or self.allowed_uid < 0
        ):
            raise ValueError("allowed_uid must be a nonnegative integer")
        if self.socket_gid is not None and (
            isinstance(self.socket_gid, bool)
            or not isinstance(self.socket_gid, int)
            or self.socket_gid < 0
        ):
            raise ValueError("socket_gid must be a nonnegative integer or None")
        if (
            isinstance(self.shutdown_timeout_seconds, bool)
            or not isinstance(self.shutdown_timeout_seconds, int)
            or not 1 <= self.shutdown_timeout_seconds <= _MAX_SHUTDOWN_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "shutdown_timeout_seconds must be between 1 and "
                f"{_MAX_SHUTDOWN_TIMEOUT_SECONDS}"
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleet-managed-projection")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--allowed-uid", type=int, required=True)
    parser.add_argument(
        "--socket-gid",
        type=int,
        default=argparse.SUPPRESS,
        help="optional group ID for a 0660 cross-UID control socket",
    )
    parser.add_argument(
        "--shutdown-timeout",
        type=int,
        default=20,
        choices=range(1, _MAX_SHUTDOWN_TIMEOUT_SECONDS + 1),
        metavar="SECONDS",
    )
    parser.add_argument(
        "--log-level",
        choices=("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"),
        default="INFO",
    )
    return parser


def _config_from_args(args: argparse.Namespace) -> ManagedServiceConfig:
    return ManagedServiceConfig(
        socket_path=args.socket,
        database_path=args.database,
        allowed_uid=args.allowed_uid,
        shutdown_timeout_seconds=args.shutdown_timeout,
        socket_gid=getattr(args, "socket_gid", None),
    )


def _projection_factory(*, database_path: Path) -> object:
    """Build the projection collaborator only in the production entrypoint."""
    from .managed_projection import ManagedProjectionStore

    return ManagedProjectionStore(database_path)


def _control_factory(
    *,
    socket_path: Path,
    allowed_uid: int,
    socket_gid: int | None,
    managed_projection: Any,
) -> object:
    """Build the authenticated local-control collaborator lazily."""
    from .local_control import LocalControlServer

    return LocalControlServer(
        socket_path=socket_path,
        allowed_uid=allowed_uid,
        socket_gid=socket_gid,
        managed_projection=managed_projection,
    )


async def run_managed_service(
    config: ManagedServiceConfig,
    *,
    projection_factory: Callable[..., object] = _projection_factory,
    control_factory: Callable[..., object] = _control_factory,
    shutdown: asyncio.Event,
) -> None:
    """Start local control, wait for shutdown, then close both collaborators."""
    managed_projection: object | None = None
    control: object | None = None
    shutdown_wait: asyncio.Task[bool] | None = None
    try:
        _validate_preprovisioned_paths(config)
        managed_projection = projection_factory(database_path=config.database_path)
        control = control_factory(
            socket_path=config.socket_path,
            allowed_uid=config.allowed_uid,
            socket_gid=config.socket_gid,
            managed_projection=managed_projection,
        )
        await _call_lifecycle(control, "start")
        logger.info("fleet managed projection ready socket=%s", config.socket_path)
        shutdown_wait = asyncio.create_task(
            shutdown.wait(), name="fleet-managed-projection-shutdown"
        )
        await shutdown_wait
    finally:
        if shutdown_wait is not None:
            shutdown_wait.cancel()
            await asyncio.gather(shutdown_wait, return_exceptions=True)
        await _close_bounded(control, config.shutdown_timeout_seconds, "local control")
        await _close_bounded(
            managed_projection, config.shutdown_timeout_seconds, "managed projection"
        )


def _validate_preprovisioned_paths(config: ManagedServiceConfig) -> None:
    """Reject unsafe parents before a database file or control socket is created."""
    from .local_control import _require_safe_socket_parent
    from .managed_projection import _require_safe_database_parent

    _require_safe_socket_parent(config.socket_path, config.socket_gid)
    _require_safe_database_parent(config.database_path)


async def _close_bounded(
    instance: object | None, timeout_seconds: int, collaborator: str
) -> None:
    if instance is None:
        return
    try:
        await asyncio.wait_for(_call_lifecycle(instance, "close"), timeout_seconds)
    except TimeoutError:
        logger.error("timed out closing %s", collaborator)


async def _call_lifecycle(instance: object, method_name: str) -> None:
    method = getattr(instance, method_name, None)
    if not callable(method):
        if method_name == "close":
            return
        raise RuntimeError(f"{method_name} lifecycle method is required")
    if inspect.iscoroutinefunction(method):
        await method()
        return
    result = await asyncio.to_thread(method)
    if inspect.isawaitable(result):
        await result


def _install_shutdown_handlers(loop: object, shutdown: asyncio.Event) -> None:
    """Turn only normal foreground termination signals into graceful shutdown."""
    add_handler = getattr(loop, "add_signal_handler", None)
    if not callable(add_handler):
        return
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            add_handler(signum, shutdown.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX fallback
            continue


async def _async_main(args: argparse.Namespace) -> None:
    shutdown = asyncio.Event()
    _install_shutdown_handlers(asyncio.get_running_loop(), shutdown)
    await run_managed_service(_config_from_args(args), shutdown=shutdown)


def main() -> None:
    args = _parser().parse_args()
    log_levels = {
        "CRITICAL": logging.CRITICAL,
        "ERROR": logging.ERROR,
        "WARNING": logging.WARNING,
        "INFO": logging.INFO,
        "DEBUG": logging.DEBUG,
    }
    logging.basicConfig(
        level=log_levels[args.log_level],
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(_async_main(args))
    except (RuntimeError, ValueError) as error:
        logger.error("fleet managed projection failed: %s", error)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
