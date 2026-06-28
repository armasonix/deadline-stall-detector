"""Stall Detector — JobSnapshot, StallHistory, StallDetector.check()

Двойной сигнал зависания:
  1. Прогресс джоба не изменился с прошлого снапшота
  2. Нет новых файлов в output_dir за период stall_threshold_min

Только детекция — recovery вынесен в recovery.py.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class JobSnapshot:
    """Снапшот рендерящегося джоба в момент поллинга."""
    job_id: str
    name: str
    progress: float          # 0.0 – 100.0
    output_dir: str
    worker: Optional[str]
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class StallHistory:
    """История зависаний одного джоба. stall_count определяет тир эскалации."""
    job_id: str
    stall_count: int = 0
    failed_workers: List[str] = field(default_factory=list)
    last_snapshot: Optional[JobSnapshot] = None


@dataclass
class StallDetector:
    """
    Детектор зависаний. Не вызывает Deadline API напрямую —
    принимает con снаружи, чтобы его можно было мокировать в тестах.
    """
    con: object
    stall_threshold_min: int = 20

    _snapshots: Dict[str, JobSnapshot] = field(default_factory=dict)
    _history: Dict[str, StallHistory] = field(default_factory=dict)

    def check(self) -> List[StallHistory]:
        """
        Один цикл проверки. Возвращает список StallHistory джобов,
        у которых зафиксировано зависание (stall_count увеличен).
        """
        current_jobs = self._fetch_rendering_jobs()
        stalled: List[StallHistory] = []
        now = datetime.utcnow()

        for snap in current_jobs:
            prev = self._snapshots.get(snap.job_id)

            if prev is None:
                # Первый раз видим джоб — записываем baseline, не детектируем
                self._snapshots[snap.job_id] = snap
                self._history.setdefault(snap.job_id, StallHistory(job_id=snap.job_id))
                log.debug("Baseline captured for job %s (%s)", snap.job_id, snap.name)
                continue

            elapsed = now - prev.timestamp
            if elapsed < timedelta(minutes=self.stall_threshold_min):
                # Ещё не прошло достаточно времени — пропускаем
                continue

            progress_moved = snap.progress > prev.progress
            new_files = self._new_files_exist(snap.output_dir, prev.timestamp)

            if progress_moved or new_files:
                # Прогресс есть — сбрасываем счётчик, обновляем снапшот
                history = self._history[snap.job_id]
                if history.stall_count > 0:
                    log.info("Job %s recovered (progress=%.1f%%)", snap.job_id, snap.progress)
                    history.stall_count = 0
                self._snapshots[snap.job_id] = snap
            else:
                # Оба сигнала: нет прогресса + нет файлов → stall
                history = self._history[snap.job_id]
                history.stall_count += 1
                history.last_snapshot = snap

                # Запоминаем воркера если он новый
                if snap.worker and snap.worker not in history.failed_workers:
                    history.failed_workers.append(snap.worker)

                log.warning(
                    "STALL detected: job=%s name=%s stall_count=%d worker=%s",
                    snap.job_id, snap.name, history.stall_count, snap.worker
                )
                stalled.append(history)
                # Обновляем снапшот чтобы не считать снова за тот же период
                self._snapshots[snap.job_id] = snap

        # Чистим историю завершённых джобов
        active_ids = {s.job_id for s in current_jobs}
        for jid in list(self._snapshots.keys()):
            if jid not in active_ids:
                del self._snapshots[jid]
                self._history.pop(jid, None)

        return stalled

    # ── private ──────────────────────────────────────────────────────────────

    def _fetch_rendering_jobs(self) -> List[JobSnapshot]:
        """Получить список джобов со статусом Rendering из Deadline."""
        try:
            jobs = self.con.Jobs.GetJobs()
        except Exception as exc:
            log.error("Failed to fetch jobs from Deadline: %s", exc)
            return []

        result = []
        for job in jobs:
            props = job.get("Props", {})
            # Stat=3 → Rendering в Deadline 10.x
            if props.get("Stat", -1) != 3:
                continue

            job_id = job.get("_id", "")
            output_dirs = props.get("OutDir", [])
            output_dir = output_dirs[0] if output_dirs else ""

            # Прогресс: завершённые задачи / всего задач * 100
            completed = props.get("Comp", 0)
            total = max(props.get("Tasks", 1), 1)
            progress = round(completed / total * 100, 2)

            worker = self._get_active_worker(job_id)

            result.append(JobSnapshot(
                job_id=job_id,
                name=props.get("Name", job_id),
                progress=progress,
                output_dir=output_dir,
                worker=worker,
            ))

        return result

    def _get_active_worker(self, job_id: str) -> Optional[str]:
        """Вернуть имя воркера, рендерящего таску джоба прямо сейчас."""
        try:
            for task in self.con.Tasks.GetJobTasks(job_id):
                if task.get("Stat", "") == "Rendering":
                    return task.get("SlaveRend") or None
        except Exception:
            pass
        return None

    def _new_files_exist(self, output_dir: str, since: datetime) -> bool:
        """
        Проверить, появились ли новые файлы в output_dir после since.
        Возвращает True если директория недоступна (не считаем зависанием).
        """
        if not output_dir:
            return True  # нет output_dir → не блокируем детекцию

        try:
            with os.scandir(output_dir) as entries:
                for entry in entries:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    mtime = datetime.utcfromtimestamp(entry.stat().st_mtime)
                    if mtime > since:
                        return True
            return False
        except (FileNotFoundError, PermissionError, OSError):
            # Директория недоступна — считаем что файлы есть, не блокируем
            log.debug("Output dir not accessible: %s", output_dir)
            return True
