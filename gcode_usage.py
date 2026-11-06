"""Per-filament usage read from a sliced file (.gcode or .gcode.3mf).

OrcaSlicer (and the Bambu/Prusa family) write the totals at the end of the
G-code, one value per filament slot of the project:

    ; filament used [mm] = 698.29, 0.00, 2985.20, 0.00
    ; filament_colour = #8EC9E6;#F2A7B5;#FA6173;#F1ECE2
    ; filament_type = PLA;PLA;PLA;PLA
    ; filament_map = 1,1,3,1

A .gcode.3mf carries the same G-code as Metadata/plate_<n>.gcode.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field

_USED_MM = re.compile(r"^;\s*filament used \[mm\]\s*=\s*(.+)$", re.IGNORECASE)
_SETTING = re.compile(r"^;\s*(filament_colour|filament_type|filament_map|extruder_colour)\s*=\s*(.*)$")


@dataclass
class FilamentUse:
    index: int            # 1-based filament number in the project
    length_mm: float
    color: str = ""       # "#RRGGBB" upper case, "" when unknown
    material: str = ""
    head: int | None = None  # 1-based tool head from filament_map, when present


@dataclass
class SlicedUsage:
    filaments: list[FilamentUse] = field(default_factory=list)

    @property
    def used(self) -> list[FilamentUse]:
        return [f for f in self.filaments if f.length_mm > 0]

    @property
    def total_mm(self) -> float:
        return sum(f.length_mm for f in self.filaments)


def _split(value: str, sep: str) -> list[str]:
    return [v.strip().strip('"') for v in value.split(sep)]


def _color(value: str) -> str:
    v = value.strip().upper()
    if re.fullmatch(r"#?[0-9A-F]{6}([0-9A-F]{2})?", v):
        v = v if v.startswith("#") else "#" + v
        return v[:7]
    return ""


def parse_gcode_text(text: str) -> SlicedUsage | None:
    """Usage from G-code text; None when the file carries no usage block."""
    used: list[float] | None = None
    settings: dict[str, str] = {}
    # The block sits at the end; scanning backwards keeps this fast on big files.
    for line in reversed(text.splitlines()):
        if used is None:
            m = _USED_MM.match(line)
            if m:
                try:
                    used = [float(x) for x in _split(m.group(1), ",")]
                except ValueError:
                    used = None
                continue
        m = _SETTING.match(line)
        if m and m.group(1) not in settings:
            settings[m.group(1)] = m.group(2)
        if used is not None and len(settings) == 4:
            break
    if not used:
        return None
    colors = _split(settings.get("filament_colour", ""), ";")
    types = _split(settings.get("filament_type", ""), ";")
    heads_raw = _split(settings.get("filament_map", ""), ",")
    out = SlicedUsage()
    for i, mm in enumerate(used):
        head = None
        if i < len(heads_raw) and heads_raw[i].isdigit():
            head = int(heads_raw[i])
        out.filaments.append(
            FilamentUse(
                index=i + 1,
                length_mm=mm,
                color=_color(colors[i]) if i < len(colors) else "",
                material=types[i].upper() if i < len(types) else "",
                head=head,
            )
        )
    return out


def parse_sliced_file(data: bytes, plate: int | None = None) -> SlicedUsage | None:
    """Usage from the bytes of a .gcode or .gcode.3mf file."""
    if data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = sorted(n for n in z.namelist() if re.fullmatch(r"Metadata/plate_\d+\.gcode", n))
                if not names:
                    return None
                if plate is not None and f"Metadata/plate_{plate}.gcode" in names:
                    names = [f"Metadata/plate_{plate}.gcode"]
                if len(names) > 1:
                    # Several plates and no hint: the printer prints the first one.
                    names = names[:1]
                return parse_gcode_text(z.read(names[0]).decode("utf-8", "replace"))
        except zipfile.BadZipFile:
            return None
    return parse_gcode_text(data.decode("utf-8", "replace"))
