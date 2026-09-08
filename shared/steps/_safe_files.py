"""Descriptor-relative reads across the quiescent agent boundary."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def open_directory_nofollow(path: Path) -> int:
    """Open an absolute directory without following any path component."""
    if not path.is_absolute():
        raise ValueError("export paths must be absolute")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def read_regular_nofollow(path: Path, *, max_bytes: int) -> bytes | None:
    """Read one bounded regular file, or ``None`` when it does not exist."""
    try:
        parent = open_directory_nofollow(path.parent)
    except FileNotFoundError:
        return None
    try:
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent,
            )
        except FileNotFoundError:
            return None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"agent export is not a regular file: {path}")
            if metadata.st_size > max_bytes:
                raise ValueError(f"agent export exceeds {max_bytes} bytes: {path}")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            body = b"".join(chunks)
            if len(body) > max_bytes:
                raise ValueError(f"agent export exceeds {max_bytes} bytes: {path}")
            return body
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)
