"""Append-only JSONL audit log.

Every plan lifecycle event, guardrail refusal, retry, and auth failure lands
here. A mutation that cannot be audited is not applied (fail closed); reads
continue — a broken audit disk never blinds monitoring.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

from ads_mcp.errors import ToolError


def _private_flags() -> int:
    # A path check followed by an ordinary open would allow a replacement
    # symlink. NONBLOCK also makes opening a replaced FIFO safe with a reader.
    if not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_NONBLOCK", "fchmod")):
        raise OSError(errno.ENOTSUP, "secure audit file operations unavailable")
    return os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK


def _secure_regular(fd: int) -> None:
    mode = os.fstat(fd).st_mode
    if not stat.S_ISREG(mode):
        raise OSError(errno.EINVAL, "audit destination must be a regular file")
    if stat.S_IMODE(mode) != 0o600:
        os.fchmod(fd, 0o600)
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            raise OSError(errno.EACCES, "audit permissions could not be secured")


def audit_writable(path) -> bool:
    """Assess readiness without creating a file or appending probe bytes.

    Existing files use the writer's secure open and permission normalization.
    A missing leaf uses parent access only; readiness cannot guarantee that a
    later creation or write succeeds, so the writer remains authoritative.
    """
    path = Path(path).absolute()
    try:
        flags = _private_flags()
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return path.parent.is_dir() and os.access(path.parent, os.W_OK | os.X_OK)
        if not stat.S_ISREG(mode):
            return False
        fd = os.open(path, flags)
        try:
            _secure_regular(fd)
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def _append_private(path: Path, data: bytes) -> None:
    """Validate and secure the opened inode before appending any bytes."""
    fd = os.open(path, _private_flags() | os.O_CREAT, 0o600)
    try:
        _secure_regular(fd)
        # One append syscall keeps successful concurrent records together on
        # local filesystems. Never truncate or roll back previously stored bytes.
        if os.write(fd, data) != len(data):
            raise OSError(errno.EIO, "incomplete audit append")
    finally:
        os.close(fd)


class AuditLog:
    def __init__(self, path, *, clock=None):
        self.path = Path(path)
        self.clock = clock if clock is not None else time.time

    def write(self, record: dict, *, critical: bool = False) -> bool:
        """Append one JSONL record. ``critical=True`` (plan lifecycle) raises
        AUDIT_WRITE_FAILED on failure; observational events (retry,
        auth_failure) swallow failures so reads keep working."""
        payload = {
            "ts": datetime.fromtimestamp(self.clock(), tz=timezone.utc).isoformat(),
            **record,
        }
        try:
            _append_private(self.path, (json.dumps(payload) + "\n").encode("utf-8"))
            return True
        except OSError as exc:
            if critical:
                raise ToolError(
                    "AUDIT_WRITE_FAILED",
                    f"audit log {self.path} is not writable "
                    f"({type(exc).__name__}); refusing to proceed with an "
                    "unauditable mutation",
                ) from None
            return False
