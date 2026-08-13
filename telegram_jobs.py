#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Cola de un job a la vez + captura de stdout para el bot de Telegram."""

from __future__ import annotations

import io
import queue
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Job:
    job_id: int
    name: str  # op1 | op2 | op3 | op4 | op5 | op9
    correos: list[str]
    kwargs: dict[str, Any] = field(default_factory=dict)
    chat_id: int | None = None
    status: str = "queued"  # queued|running|done|error|cancelled
    result: dict[str, Any] | None = None
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    log_lines: list[str] = field(default_factory=list)


class _TeeStdout:
    """Duplica writes a stdout real + buffer de líneas para Telegram."""

    def __init__(self, real, on_line: Callable[[str], None]):
        self._real = real
        self._on_line = on_line
        self._buf = ""

    def write(self, s):
        if not s:
            return 0
        try:
            self._real.write(s)
        except Exception:
            pass
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.replace("\r", "").rstrip()
            if line:
                try:
                    self._on_line(line)
                except Exception:
                    pass
        return len(s)

    def flush(self):
        try:
            self._real.flush()
        except Exception:
            pass

    def isatty(self):
        return False


class JobQueue:
    """Una sola cola: no lanza otro Chromium mientras hay job activo."""

    def __init__(self, log_callback: Callable[[Job, str], None] | None = None):
        self._q: queue.Queue[Job | None] = queue.Queue()
        self._lock = threading.Lock()
        self._next_id = 1
        self._current: Job | None = None
        self._cancel_flag = threading.Event()
        self._log_callback = log_callback
        self._worker = threading.Thread(target=self._run_loop, name="tidal-job-worker", daemon=True)
        self._worker.start()

    def cancel_check(self) -> bool:
        return self._cancel_flag.is_set()

    def request_cancel(self) -> bool:
        with self._lock:
            if self._current and self._current.status == "running":
                self._cancel_flag.set()
                self._current.status = "cancelled"
                return True
        return False

    def status_snapshot(self) -> dict[str, Any]:
        with self._lock:
            cur = self._current
            pending = self._q.qsize()
        if not cur:
            return {"current": None, "pending": pending}
        return {
            "current": {
                "job_id": cur.job_id,
                "name": cur.name,
                "status": cur.status,
                "correos": len(cur.correos),
                "started_at": cur.started_at,
                "recent_logs": cur.log_lines[-12:],
            },
            "pending": pending,
        }

    def submit(
        self,
        name: str,
        correos: list[str],
        *,
        chat_id: int | None = None,
        **kwargs,
    ) -> Job:
        with self._lock:
            jid = self._next_id
            self._next_id += 1
            job = Job(job_id=jid, name=name, correos=list(correos), kwargs=dict(kwargs), chat_id=chat_id)
        self._q.put(job)
        return job

    def _append_log(self, job: Job, line: str) -> None:
        # Evitar filtrar secretos obvios
        low = line.lower()
        if "password" in low and ("app " in low or ":" in line):
            if len(line) > 80:
                line = line[:40] + " … [redacted]"
        job.log_lines.append(line)
        if len(job.log_lines) > 400:
            job.log_lines = job.log_lines[-300:]
        if self._log_callback:
            try:
                self._log_callback(job, line)
            except Exception:
                pass

    def _run_loop(self) -> None:
        import telegram_runners as runners

        runners_map = {
            "op1": runners.ejecutar_opcion1,
            "op2": runners.ejecutar_opcion2,
            "op3": runners.ejecutar_opcion3,
            "op4": runners.ejecutar_opcion4,
            "op5": runners.ejecutar_opcion5,
            "op9": runners.ejecutar_opcion9,
            "op12": runners.ejecutar_opcion12,
        }
        while True:
            job = self._q.get()
            if job is None:
                break
            self._cancel_flag.clear()
            with self._lock:
                self._current = job
                job.status = "running"
                job.started_at = time.time()

            def on_line(line: str, _job=job):
                self._append_log(_job, line)

            old_out, old_err = sys.stdout, sys.stderr
            tee = _TeeStdout(old_out, on_line)
            sys.stdout = tee  # type: ignore
            sys.stderr = tee  # type: ignore
            try:
                fn = runners_map.get(job.name)
                if not fn:
                    raise ValueError(f"Job desconocido: {job.name}")
                kwargs = dict(job.kwargs)
                kwargs.setdefault("headless", False)
                kwargs["cancel_check"] = self.cancel_check
                result = fn(job.correos, **kwargs)
                job.result = result if isinstance(result, dict) else {"raw": result}
                if job.status != "cancelled":
                    job.status = "done"
            except Exception as e:
                job.error = f"{type(e).__name__}: {e}"
                job.status = "error"
                tb = traceback.format_exc()
                for line in tb.splitlines():
                    self._append_log(job, line)
            finally:
                sys.stdout = old_out
                sys.stderr = old_err
                job.finished_at = time.time()
                with self._lock:
                    # Mantener current hasta que el bot lea el resultado; se limpia al encolar siguiente
                    pass
                # Señal de fin vía log especial
                self._append_log(job, f"__JOB_DONE__:{job.job_id}:{job.status}")
                with self._lock:
                    self._current = None
                self._q.task_done()
