"""Find the printer's serial number and check code without anyone typing them.

FlashForge WebUI keeps every printer it has connected to in
printer_details.json ({"lastUsedPrinterSerial", "printers": {serial: {...,
"SerialNumber", "CheckCode", "IPAddress"}}}). When that folder is mounted, the
service takes the values from there once and keeps its own copy in
DATA_DIR/printer.json, so the WebUI folder can be deleted afterwards. Any JSON
file in the folder is searched, so older layouts (config.json) work as well.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("c5p-spoolman")


def _find(obj: Any, out: list[dict[str, str]]) -> None:
    """Collect every object that carries both a serial number and a check code."""
    if isinstance(obj, dict):
        keys = {k.lower().replace("_", ""): k for k in obj}
        if "serialnumber" in keys and "checkcode" in keys:
            serial, code = obj[keys["serialnumber"]], obj[keys["checkcode"]]
            if isinstance(serial, str) and isinstance(code, str) and serial and code and code != "123":
                ip = next((obj[keys[k]] for k in ("ipaddress", "ip", "host") if k in keys), "")
                out.append({"serial": serial, "check_code": code, "host": str(ip or "")})
        for v in obj.values():
            _find(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _find(v, out)


def from_folder(folder: Path, host: str = "") -> tuple[dict[str, str] | None, str]:
    """The printer's credentials from a FlashForge WebUI data folder, or why not."""
    if not folder.is_dir():
        return None, f"{folder} is not there"
    found: list[dict[str, str]] = []
    last_used = None
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            log.warning("cannot read %s: %s", path, e)
            continue
        if isinstance(data, dict) and data.get("lastUsedPrinterSerial"):
            last_used = data["lastUsedPrinterSerial"]
        _find(data, found)
    unique = list({p["serial"]: p for p in found}.values())
    if not unique:
        return None, f"no printer with a serial number and check code in {folder}"
    for rule, pick in (("its IP", lambda p: host and p["host"] == host),
                       ("the last one used", lambda p: p["serial"] == last_used),
                       ("the only one", lambda p: len(unique) == 1)):
        hits = [p for p in unique if pick(p)]
        if len(hits) == 1:
            return hits[0], f"{folder} ({rule})"
    return None, f"{len(unique)} printers in {folder}; set PRINTER_SERIAL to say which"


def resolve(serial: str, check_code: str, host: str, data_dir: Path, import_dirs: list[Path]) -> tuple[str, str, str, str]:
    """serial, check code, host and where they came from: settings, the saved copy or an import."""
    if serial and check_code:
        return serial, check_code, host, "settings"
    saved = data_dir / "printer.json"
    try:
        p = json.loads(saved.read_text())
        if p.get("serial") and p.get("check_code") and (not serial or serial == p["serial"]):
            return p["serial"], p["check_code"], host or p.get("host", ""), str(saved)
    except (OSError, ValueError):
        pass
    reasons = []
    for folder in import_dirs:
        p, why = from_folder(folder, host)
        if p and (not serial or serial == p["serial"]):
            tmp = saved.with_suffix(".tmp")
            tmp.write_text(json.dumps(p))
            tmp.replace(saved)
            log.info("printer %s: serial number and check code taken from %s", p["serial"], why)
            return p["serial"], check_code or p["check_code"], host or p["host"], why
        reasons.append(why)
    return serial, check_code, host, "; ".join(reasons)
