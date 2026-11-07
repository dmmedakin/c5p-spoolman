"""Work out which Spoolman spool sits in a material-station slot.

The Creator 5 Pro has no RFID: when a spool is loaded, the printer asks on its
screen for the material and the colour, and reports them per slot. That pair
is matched against the spools in Spoolman: same material family, nearest
colour. Several spools of one filament (two identical black PETG) are the same
choice for accounting, so an opened one is preferred and there is nothing to
ask. Nothing is guessed: two different filaments that both match are a question
for the user, and no match at all means the spool is not in Spoolman yet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from allocate import _rgb

# Material families, longest first so PETG is not read as PET.
FAMILIES = ("PETG", "PCTG", "PLA", "ABS", "ASA", "TPU", "PET", "PVA", "HIPS", "PA", "PC", "PP")
# Colour distances are CIELAB delta E (CIE76): about 2 is barely visible, 10 is
# a clearly different shade. Measured on real loads: the printer's white to a
# white spool 4.5, to a clear one 6.7; black to black 0.5; while its coral
# default to a rose and to a red spool was 42 and 25. A spool is a match only
# within this distance.
MATCH_DISTANCE = 12.0


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
    """spool: the one match. candidates: several different filaments match, ask which."""
    spool: dict[str, Any] | None
    distance: float | None = None
    candidates: list[tuple[dict[str, Any], float]] = field(default_factory=list)
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
    """The spool for a slot reporting material and colour, the ones to ask about, or none.

    taken: spools assigned to other slots, never chosen twice.
    already_here: a spool whose Spoolman location already names this slot (set
    by hand or earlier); it wins when it fits the material.
    """
    fam = family(material)
    usable = [s for s in spools if not s.get("archived") and int(s["id"]) not in taken
              and family((s.get("filament") or {}).get("material", "")) == fam]
    if already_here is not None:
        for s in usable:
            if int(s["id"]) == already_here:
                return Choice(s, _distance(color, _color_of(s)), reason="its location in Spoolman names this slot")
    # One entry per filament: its preferred spool and that filament's distance.
    by_filament: dict[int, tuple[float, dict[str, Any]]] = {}
    for s in usable:
        d = _distance(color, _color_of(s))
        if d is None or d > MATCH_DISTANCE:
            continue
        fid = int((s.get("filament") or {}).get("id") or -int(s["id"]))
        cur = by_filament.get(fid)
        if cur is None or _preference(s) < _preference(cur[1]):
            by_filament[fid] = (d, s)
    ranked = sorted(by_filament.values(), key=lambda t: (t[0], _preference(t[1])))
    if not ranked:
        return Choice(None, reason=f"no {fam or 'such'} spool of this colour in Spoolman")
    if len(ranked) == 1:
        return Choice(ranked[0][1], ranked[0][0], reason="the only matching spool")
    return Choice(None, candidates=[(s, d) for d, s in ranked[:3]],
                  reason=f"{len(ranked)} different spools match")


# Plain names for the colours a printer's palette and spools use.
_NAMES = {
    "white": "FFFFFF", "light grey": "C8C8C8", "grey": "8C8C8C", "dark grey": "4A4A4A", "black": "000000",
    "red": "D01C1C", "dark red": "7A1020", "coral": "F7706A", "pink": "F4A6B8", "magenta": "D0308C",
    "orange": "F08020", "yellow": "F5D000", "beige": "E3D3B4", "brown": "7A4A2A", "olive": "7A7A20",
    "light green": "90D070", "green": "20A040", "dark green": "1F5A30", "teal": "20A0A0",
    "light blue": "90C8E8", "blue": "2060D0", "navy": "1A2A6A", "purple": "6A3A9A", "lilac": "C0A0D8",
}


def color_name(color: str) -> str:
    """'#FFFFFF' -> 'white (#FFFFFF)'."""
    c = (color or "").upper()
    if not _lab(c):
        return c or "no colour"
    name = min(_NAMES, key=lambda n: _distance(c, _NAMES[n]))
    return f"{name} ({c if c.startswith('#') else '#' + c})"


def describe(spool: dict[str, Any]) -> str:
    f = spool.get("filament") or {}
    vendor = (f.get("vendor") or {}).get("name")
    name = f.get("name") or f"spool {spool['id']}"
    return f"#{spool['id']} {vendor + ' ' if vendor else ''}{name}"
