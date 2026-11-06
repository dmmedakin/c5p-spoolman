"""Filament bookkeeping for the Creator 5 Pro, independent of any network I/O.

The printer reports a single lifetime counter, `cumulativeFilament` (metres,
every head together), plus the material-station slot that is feeding now. The
tracker turns successive readings into metres per slot, per job:

* every reading's rise of the counter is credited to the slot feeding at that
  moment (or kept as unattributed when the printer names none);
* a job runs from the printer entering an active state with a file name until
  it leaves the active states; its readings collect in `job`;
* outside a job, a rising counter (loading, unloading, manual purges) is idle
  use, credited to the slot that fed it.

Splitting a finished job between slots using the slicer's numbers, and talking
to Spoolman, happen in the service; this module only measures.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# Printer states in which a job is under way (FlashForge HTTP API `status`).
ACTIVE_STATES = {"printing", "pausing", "paused", "pause", "heating", "busy", "calibrate_doing"}
# How a job ended, by the first non-active state the printer reports after it.
END_STATES = {"completed": "completed", "cancel": "cancelled", "cancelled": "cancelled", "error": "failed"}

# A job whose print duration already exceeds this when first seen started
# before we were watching: its start of the counter is unknown.
MISSED_START_S = 120
# Fastest believable filament rate, metres per second, all heads together.
# Generous on purpose (fast printing plus purge); it only rejects counter
# glitches, which otherwise would be charged to a spool.
MAX_RATE_M_PER_S = 0.05
# Smallest allowance per reading, so short poll gaps never reject real use.
MIN_ALLOWANCE_M = 2.0


@dataclass
class Slot:
    id: int
    material: str = ""
    color: str = ""
    loaded: bool = False


@dataclass
class Snapshot:
    """One normalised /detail reading."""

    status: str
    file: str
    cumulative_m: float | None
    current_slot: int  # 0 when the printer names no slot
    duration_s: float = 0.0
    progress: float = 0.0  # 0..1
    slots: list[Slot] = field(default_factory=list)
    nozzle_targets: list[float] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATES


def snapshot_from_detail(d: dict[str, Any]) -> Snapshot:
    """Normalise the printer's raw /detail payload."""

    def num(key: str, default: float = 0.0) -> float:
        try:
            v = d.get(key)
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    station = d.get("matlStationInfo") or {}
    slots = [
        Slot(
            id=int(s.get("slotId") or 0),
            material=str(s.get("materialName") or "").upper(),
            color=_norm_color(str(s.get("materialColor") or "")),
            loaded=bool(s.get("hasFilament")),
        )
        for s in (station.get("slotInfos") or [])
        if s.get("slotId")
    ]
    cum = d.get("cumulativeFilament")
    try:
        cum_m = float(cum) if cum is not None else None
    except (TypeError, ValueError):
        cum_m = None
    progress = num("printProgress")
    if progress > 1.0:
        progress /= 100.0
    targets = []
    for t in d.get("nozzleTargetTemps") or []:
        try:
            targets.append(float(t))
        except (TypeError, ValueError):
            targets.append(0.0)
    try:
        current = int(station.get("currentSlot") or 0)
    except (TypeError, ValueError):
        current = 0
    return Snapshot(
        status=str(d.get("status") or "").strip().lower(),
        file=str(d.get("printFileName") or "").strip(),
        cumulative_m=cum_m,
        current_slot=current,
        duration_s=num("printDuration"),
        progress=progress,
        slots=slots,
        nozzle_targets=targets,
    )


def _norm_color(value: str) -> str:
    v = value.strip().upper().lstrip("#")
    return "#" + v[:6] if len(v) >= 6 and all(c in "0123456789ABCDEF" for c in v[:6]) else ""


def feeding_slot(snap: Snapshot, last_slot: int | None) -> int | None:
    """The slot whose filament is moving now, or None when it cannot be told.

    The printer names it in currentSlot. Mid tool change that can read 0; then
    the head with the highest target temperature is the one at work (heads
    are numbered like slots), and failing that the last slot seen.
    """
    if snap.current_slot > 0:
        return snap.current_slot
    hot = [(t, i + 1) for i, t in enumerate(snap.nozzle_targets) if t > 0]
    if hot:
        top = max(hot)
        if sum(1 for t, _ in hot if t == top[0]) == 1:
            return top[1]
    return last_slot


@dataclass
class Event:
    kind: str  # "job_finished" | "idle_use"
    data: dict[str, Any]


class Tracker:
    """Folds snapshots into per-slot metres. `state` is JSON-serialisable."""

    def __init__(self, state: dict[str, Any] | None = None, count_idle: bool = True,
                 idle_flush_m: float = 0.2) -> None:
        self.state: dict[str, Any] = state or {}
        self.state.setdefault("last_cum_m", None)
        self.state.setdefault("last_seen", None)
        self.state.setdefault("last_slot", None)
        self.state.setdefault("job", None)
        self.state.setdefault("idle_m", {})
        self.count_idle = count_idle
        self.idle_flush_m = idle_flush_m

    @property
    def job(self) -> dict[str, Any] | None:
        return self.state.get("job")

    def _delta(self, cum: float | None, now: float) -> float:
        last, seen = self.state["last_cum_m"], self.state["last_seen"]
        if cum is None:
            return 0.0
        self.state["last_cum_m"] = cum
        if last is None:
            return 0.0
        if cum < last:
            return 0.0  # counter reset or bad read: re-baseline, charge nothing
        delta = cum - last
        gap = max(0.0, now - seen) if seen else 0.0
        allowance = max(MIN_ALLOWANCE_M, gap * MAX_RATE_M_PER_S)
        return delta if delta <= allowance else 0.0

    def _credit(self, bucket: dict[str, float], slot: int | None, metres: float) -> None:
        key = str(slot) if slot else "unattributed"
        bucket[key] = round(bucket.get(key, 0.0) + metres, 6)

    def _start_job(self, snap: Snapshot, now: float) -> None:
        self.state["job"] = {
            "file": snap.file,
            "started_at": now,
            "partial": snap.duration_s > MISSED_START_S,
            "start_cum_m": self.state["last_cum_m"],
            "metres": {},
            "slots_seen": [s.__dict__ for s in snap.slots],
            "last_status": snap.status,
            "last_progress": snap.progress,
        }

    def _finish_job(self, outcome: str, now: float) -> Event:
        job = self.state["job"]
        job["ended_at"] = now
        job["outcome"] = outcome
        self.state["job"] = None
        return Event("job_finished", job)

    def _flush_idle(self, force: bool = False) -> list[Event]:
        events = []
        for key, metres in list(self.state["idle_m"].items()):
            if metres <= 0:
                continue
            if force or metres >= self.idle_flush_m:
                events.append(Event("idle_use", {"slot": key, "metres": metres}))
                self.state["idle_m"][key] = 0.0
        self.state["idle_m"] = {k: v for k, v in self.state["idle_m"].items() if v > 0}
        return events

    def observe(self, snap: Snapshot, now: float | None = None) -> list[Event]:
        """Fold one reading in; returns what became final with it."""
        now = time.time() if now is None else now
        events: list[Event] = []
        delta = self._delta(snap.cumulative_m, now)
        slot = feeding_slot(snap, self.state["last_slot"])
        job = self.job

        # A different file while active: the previous job ended unseen.
        if job and snap.active and snap.file and snap.file != job["file"]:
            events.append(self._finish_job("ended", now))
            job = None

        if job:
            # Counter readings that arrive with the end state still belong to the job.
            self._credit(job["metres"], slot, delta)
            if snap.active:
                job["last_status"] = snap.status
                job["last_progress"] = snap.progress
                if snap.slots:
                    job["slots_seen"] = [s.__dict__ for s in snap.slots]
            else:
                events.append(self._finish_job(END_STATES.get(snap.status, "ended"), now))
        elif snap.active and snap.file:
            events.extend(self._flush_idle(force=True))
            self._start_job(snap, now)
            # The rise since the last reading happened before or at the start;
            # a partial job only counts what is seen from here on.
            if delta and not self.job["partial"]:
                self._credit(self.job["metres"], slot, delta)
        elif delta > 0 and self.count_idle:
            self._credit(self.state["idle_m"], slot, delta)
            events.extend(self._flush_idle())

        if slot:
            self.state["last_slot"] = slot
        self.state["last_seen"] = now
        return events
