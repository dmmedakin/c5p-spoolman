"""Turn a finished job's measurements into millimetres per material-station slot.

Measured metres are the truth for the total: the printer's counter sees every
purge and calibration the slicer cannot. How that total divides between slots
is taken from the sliced file when the job completed and the file's filaments
can be matched to the slots that actually fed; otherwise from what the poller
attributed while the job ran.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from gcode_usage import SlicedUsage

# How much of the total a slot must have fed to count as "used" when checking
# that a sliced file's filaments match what the printer did.
USED_SHARE = 0.05
USED_MIN_M = 0.02
# Largest RGB distance at which a sliced filament colour still matches a slot.
COLOR_MATCH = 80.0


@dataclass
class Allocation:
    method: str
    per_slot_mm: dict[int, float] = field(default_factory=dict)
    unassigned_mm: float = 0.0
    total_mm: float = 0.0
    notes: list[str] = field(default_factory=list)


def _base_material(name: str) -> str:
    return re.sub(r"[^A-Z]", "", (name or "").upper())


def _material_match(a: str, b: str) -> bool:
    a, b = _base_material(a), _base_material(b)
    if not a or not b:
        return True  # unknown on either side: do not veto on material
    return a == b or a.startswith(b) or b.startswith(a)


def _rgb(c: str) -> tuple[int, int, int] | None:
    c = (c or "").lstrip("#")
    if len(c) < 6:
        return None
    try:
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        return None


def _distance(a: str, b: str) -> float | None:
    x, y = _rgb(a), _rgb(b)
    if not x or not y:
        return None
    return sum((p - q) ** 2 for p, q in zip(x, y)) ** 0.5


def map_by_color(sliced: SlicedUsage, slots: list[dict[str, Any]]) -> dict[int, int] | None:
    """Filament index -> slot id, matching material and nearest colour; None if ambiguous."""
    loaded = [s for s in slots if s.get("loaded")]
    mapping: dict[int, int] = {}
    for f in sliced.used:
        best: tuple[float, int] | None = None
        for s in loaded:
            if not _material_match(f.material, s.get("material", "")):
                continue
            d = _distance(f.color, s.get("color", ""))
            if d is None or d > COLOR_MATCH:
                continue
            if best is None or d < best[0]:
                best = (d, int(s["id"]))
        if best is None:
            return None
        mapping[f.index] = best[1]
    if len(set(mapping.values())) != len(mapping):
        return None  # two filaments on one slot: colours too close to tell apart
    return mapping


def map_by_head(sliced: SlicedUsage, slots: list[dict[str, Any]]) -> dict[int, int] | None:
    """Filament index -> slot id from the file's filament_map (head n is fed by slot n)."""
    ids = {int(s["id"]) for s in slots} or {1, 2, 3, 4}
    mapping = {}
    for f in sliced.used:
        if f.head is None or f.head not in ids:
            return None
        mapping[f.index] = f.head
    if len(set(mapping.values())) != len(mapping):
        return None
    return mapping


def allocate(job: dict[str, Any], sliced: SlicedUsage | None) -> Allocation:
    metres: dict[str, float] = job.get("metres") or {}
    per_slot = {int(k): v for k, v in metres.items() if k != "unattributed" and v > 0}
    unattributed = float(metres.get("unattributed", 0.0))
    total_m = sum(per_slot.values()) + unattributed
    out = Allocation(method="none", total_mm=round(total_m * 1000, 2))
    if total_m <= 0:
        out.notes.append("The printer's counter did not move during this job.")
        return out
    if job.get("partial"):
        out.notes.append("The job started before the service was watching; only the part seen is counted.")

    slots = job.get("slots_seen") or []
    used_slots = {s for s, m in per_slot.items() if m >= max(USED_SHARE * total_m, USED_MIN_M)}

    if sliced and sliced.used:
        for name, mapper in (("colour", map_by_color), ("filament_map", map_by_head)):
            mapping = mapper(sliced, slots)
            if not mapping:
                continue
            agrees = set(mapping.values()) == used_slots or (not used_slots and unattributed > 0)
            if job.get("outcome") == "completed" and agrees:
                used_total = sum(f.length_mm for f in sliced.used)
                for f in sliced.used:
                    slot = mapping[f.index]
                    out.per_slot_mm[slot] = round(out.per_slot_mm.get(slot, 0) + total_m * 1000 * f.length_mm / used_total, 2)
                out.method = f"measured total, split by the sliced file (matched by {name})"
                return out
            if not per_slot and agrees:
                # Nothing attributed live and the job did not complete: fall back
                # to the file's proportions rather than leave it unassigned.
                used_total = sum(f.length_mm for f in sliced.used)
                for f in sliced.used:
                    slot = mapping[f.index]
                    out.per_slot_mm[slot] = round(out.per_slot_mm.get(slot, 0) + total_m * 1000 * f.length_mm / used_total, 2)
                out.method = f"measured total, split by the sliced file (matched by {name}, job did not complete)"
                return out
        out.notes.append("The sliced file's filaments did not match the slots that fed; using live attribution.")

    if per_slot:
        attributed = sum(per_slot.values())
        for slot, m in per_slot.items():
            share = unattributed * m / attributed if unattributed else 0.0
            out.per_slot_mm[slot] = round((m + share) * 1000, 2)
        out.method = "measured per slot"
        return out

    loaded = [int(s["id"]) for s in slots if s.get("loaded")]
    if len(loaded) == 1:
        out.per_slot_mm[loaded[0]] = round(total_m * 1000, 2)
        out.method = "measured, only one slot loaded"
        return out
    out.unassigned_mm = round(total_m * 1000, 2)
    out.method = "measured, slot unknown"
    out.notes.append("The printer never named the feeding slot, so the use could not be assigned to a spool.")
    return out
