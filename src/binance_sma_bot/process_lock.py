from __future__ import annotations

from pathlib import Path

from filelock import FileLock, Timeout


class AlreadyRunningError(RuntimeError):
    pass


class ProcessLock:
    def __init__(self, path: Path):
        self.path = path
        self._lock = FileLock(str(path))

    def __enter__(self) -> "ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._lock.acquire(timeout=0)
        except Timeout as exc:
            raise AlreadyRunningError("다른 봇 프로세스가 이미 실행 중입니다.") from exc
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._lock.is_locked:
            self._lock.release()
