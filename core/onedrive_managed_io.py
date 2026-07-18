"""Bounded, ownership-guarded writes to managed CompanyA OneDrive files."""
from __future__ import annotations

import errno
import os
import time
import uuid
from pathlib import Path


OWN_ROOT = (Path.home() / "Library" / "CloudStorage" /
            "OneDrive-redactedIndustries,Inc").resolve()
_RETRY_ERRNOS = {errno.EAGAIN, errno.EBUSY, errno.ETIMEDOUT}


class ManagedOneDriveWriter:
    def __init__(self, root: Path, managed: set[Path] | None = None,
                 *, attempts: int = 4, delay: float = 0.15):
        self.root = Path(root).resolve()
        if (self.root == OWN_ROOT or OWN_ROOT not in self.root.parents
                or any(part.lower() in {"sharedlibraries", "sharepoint", "sites"}
                       for part in self.root.parts)):
            raise PermissionError("destination is not inside the owned OneDrive root")
        self.managed = {Path(p).resolve() for p in (managed or set())}
        self.attempts = max(1, attempts)
        self.delay = max(0.0, delay)

    def add(self, path: Path) -> Path:
        target = Path(path).resolve()
        if self.root not in target.parents:
            raise PermissionError("managed target escapes the sync root")
        self.managed.add(target)
        return target

    def _check(self, path: Path) -> Path:
        target = Path(path).resolve()
        if target not in self.managed or self.root not in target.parents:
            raise PermissionError("refusing to write an unmanaged OneDrive target")
        return target

    def write_bytes(self, path: Path, data: bytes) -> bool:
        target = self._check(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        last: OSError | None = None
        for attempt in range(self.attempts):
            temp = target.parent / f".{target.name}.sync-{uuid.uuid4().hex}.tmp"
            try:
                temp.write_bytes(data)
                os.replace(temp, target)
                return True
            except OSError as exc:
                last = exc
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
                if exc.errno not in _RETRY_ERRNOS:
                    raise
                if attempt + 1 < self.attempts:
                    time.sleep(self.delay * (attempt + 1))

        # File Provider can keep a stale placeholder pinned while sibling writes work.
        # The target is exact-listed above, so replacing it cannot touch user files.
        temp = target.parent / f".{target.name}.replace-{uuid.uuid4().hex}.tmp"
        try:
            temp.write_bytes(data)
            target.unlink(missing_ok=True)
            os.replace(temp, target)
            return True
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        if last:  # pragma: no cover - retained for type checkers
            raise last

    def write_text(self, path: Path, text: str) -> bool:
        return self.write_bytes(path, text.encode("utf-8"))

    def remove(self, path: Path) -> bool:
        target = self._check(path)
        if not target.exists():
            return False
        for attempt in range(self.attempts):
            try:
                target.unlink()
                return True
            except OSError as exc:
                if exc.errno not in _RETRY_ERRNOS or attempt + 1 >= self.attempts:
                    raise
                time.sleep(self.delay * (attempt + 1))
        return False
