"""Work out which Spoolman spool sits in a material-station slot.

The Creator 5 Pro has no RFID: when a spool is loaded, the printer asks on its
screen for the material and the colour, and reports them per slot. That pair
is matched against the spools in Spoolman: same material family, nearest
colour. Several spools of one filament (two identical black PETG) are the same
choice for accounting, so an opened one is preferred and there is nothing to
ask. Two different filaments close in colour are a real doubt: the best guess
is taken and the runners-up are offered for a one-tap correction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from allocate import _rgb

# Material families, longest first so PETG is not read as PET.
FAMILIES = ("PETG", "PCTG", "PLA", "ABS", "ASA", "TPU", "PET", "PVA", "HIPS", "PA", "PC", "PP")
# Colour distances are CIELAB delta E (CIE76): about 2 is barely visible, 10 is
# a clearly different shade. In plain RGB, beige sits as close to pink as two
# pinks do; in Lab it does not.
# Farther than this from the slot's colour, a spool is not a candidate at all
# (the printer's palette is coarse: its pure red is ~36 from a real red).
MAX_DISTANCE = 50.0
# A different filament within this much of the best guess makes the choice doubtful.
DOUBT_MARGIN = 12.0


def _lab(color: str) -> tuple[float, float, float] | None:
    rgb = _rgb(color)
    if not rgb:
        return None

    def lin(c: float) -> float:
        c /= 255
        return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92

    r, g, b = (lin(c) for c in rgb)
    x = (r * 0.4124 + g * 0.3576 + b * 0.1805) / 0.95047
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = (r * 0.0193 + g * 0.1192 + b * 0.9505) / 1.08883

    def f(v: float) -> float:
        return v ** (1 / 3) if v > 0.008856 else 7.787 * v + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def _distance(a: str, b: str) -> float | None:
    x, y = _lab(a), _lab(b)
    if not x or not y:
        return None
    return sum((p - q) ** 2 for p, q in zip(x, y)) ** 0.5


def family(material: str) -> str:
    """'PLA+', 'HS PLA', 'PLA Wood', 'PLA-CF' -> 'PLA'; unknown text stays as is."""
    m = re.sub(r"[^A-Z]", " ", (material or "").upper())
    for word in m.split():
        for f in FAMILIES:
            if word.startswith(f):
                return f
    return m.strip()


def slot_key(loaded: bool, material: str, color: str) -> str:
    """What identifies a load: a change of it means the spool was swapped."""
    return f"{int(bool(loaded))}|{family(material)}|{(color or '').upper()}" if loaded else "0||"


@dataclass
class Choice:
    spool: dict[str, Any] | None
    distance: float | None = None
    doubtful: bool = False
    alternatives: list[tuple[dict[str, Any], float]] = field(default_factory=list)
    reason: str = ""


def _color_of(spool: dict[str, Any]) -> str:
    f = spool.get("filament") or {}
    c = f.get("color_hex") or ""
    if not c and f.get("multi_color_hexes"):
        c = str(f["multi_color_hexes"]).split(",")[0]
    return c


def _preference(spool: dict[str, Any]) -> tuple:
    """Among spools of one filament: an opened one first, the emptiest, then the oldest."""
    used = float(spool.get("used_weight") or 0)
    remaining = spool.get("remaining_weight")
    return (0 if used > 0 else 1, float(remaining) if remaining is not None else 1e9, int(spool["id"]))


def choose(material: str, color: str, spools: list[dict[str, Any]], *,
           taken: set[int] = frozenset(), already_here: int | None = None) -> Choice:
    """Pick the spool for a slot reporting material and colour.

    taken: spools assigned to other slots, never chosen twice.
    already_here: a spool whose Spoolman location already names this slot (set
    by hand or earlier); it wins when it fits the material.
    """
    fam = family(material)
    usable = [s for s in spools if not s.get("archived") and int(s["id"]) not in taken
              and family((s.get("filament") or {}).get("material", "")) == fam]
    if not usable:
        return Choice(None, reason=f"no {fam or 'such'} spool in Spoolman")
    if already_here is not None:
        for s in usable:
            if int(s["id"]) == already_here:
                return Choice(s, _distance(color, _color_of(s)), reason="already set to this slot")
    scored: list[tuple[float, dict[str, Any]]] = []
    for s in usable:
        d = _distance(color, _color_of(s))
        if d is not None and d <= MAX_DISTANCE:
            scored.append((d, s))
    if not scored:
        return Choice(None, reason=f"no {fam} spool close to colour {color or '?'} in Spoolman")
    # One entry per filament: its best spool and that filament's distance.
    by_filament: dict[int, tuple[float, dict[str, Any]]] = {}
    for d, s in scored:
        fid = int((s.get("filament") or {}).get("id") or -int(s["id"]))
        cur = by_filament.get(fid)
        if cur is None or _preference(s) < _preference(cur[1]):
            by_filament[fid] = (d, s)
    ranked = sorted(by_filament.values(), key=lambda t: (t[0], _preference(t[1])))
    best_d, best = ranked[0]
    close = [(s, d) for d, s in ranked[1:] if d - best_d <= DOUBT_MARGIN]
    return Choice(best, best_d, doubtful=bool(close), alternatives=close[:2],
                  reason="nearest colour" + (", others are close" if close else ""))


def describe(spool: dict[str, Any]) -> str:
    f = spool.get("filament") or {}
    vendor = (f.get("vendor") or {}).get("name")
    name = f.get("name") or f"spool {spool['id']}"
    return f"#{spool['id']} {vendor + ' ' if vendor else ''}{name}"
