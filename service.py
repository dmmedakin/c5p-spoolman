"""c5p-spoolman: charge Spoolman spools with what a FlashForge Creator 5 Pro really uses.

Polls the printer's local API, measures filament per material-station slot
(tracker.py), splits each finished job between slots (allocate.py, using the
sliced file from PrintStash when it is there) and deducts the result from the
spool Spoolman has in that slot. A spool is "in slot N" when its Spoolman
location is SLOT_LOCATION with {slot} filled in, e.g. "Creator 5 Pro / Slot 2".

That location is set by the service itself (assign.py): when a slot's material
or colour changes on the printer, the matching spool is moved into the slot
and the one taken out goes back where it was. Nothing is guessed: when two
spools match, the user picks one with a tap; when none does, the user is told
to add it to Spoolman. Use from such a slot is held until its spool is known.

Settings (environment variables):
  PRINTER_HOST          printer IP or name (required)
  PRINTER_SERIAL        serial number, from the printer's network settings (required)
  PRINTER_CHECK_CODE    check code, from the same screen (required)
  PRINTER_PORT          local API port, 8898
  SPOOLMAN_URL          e.g. http://spoolman:8000 (required)
  SLOT_LOCATION         Spoolman location of slot N, "Creator 5 Pro / Slot {slot}"
  PRINTSTASH_URL        optional: where the sliced files are, e.g. http://printstash:3000
  PRINTSTASH_USERNAME   with PRINTSTASH_URL
  PRINTSTASH_API_KEY    with PRINTSTASH_URL
  NTFY_URL              optional: full topic URL for a message per job
  NTFY_TOKEN            optional ntfy access token
  PUBLIC_URL            how the phone reaches this service, e.g. http://192.168.0.96:3065;
                        enables the correction buttons in ntfy messages
  ACTION_TOKEN          secret for those buttons; generated and kept in DATA_DIR if unset
  AUTO_ASSIGN           true: match loaded spools to Spoolman and set their location
  SLOT_SETTLE_S         seconds a slot's material and colour must stay unchanged before
                        it is matched, 60
  RECHECK_S             how often a slot without a known spool is matched again, 300
  COUNT_IDLE_USE        true: also charge filament moved between jobs (loading, purges)
  POLL_INTERVAL_S       3
  DATA_DIR              /data
  PORT                  8000, for /healthz and /status
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import signal
import time
from pathlib import Path
from typing import Any

from urllib.parse import parse_qs, urlsplit

import httpx

from allocate import allocate
from assign import choose, color_name, describe, slot_key
from gcode_usage import SlicedUsage, parse_sliced_file
from tracker import Event, Snapshot, Tracker, snapshot_from_detail

log = logging.getLogger("c5p-spoolman")


def env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    return v if v is not None else ""


def env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self) -> None:
        self.printer_host = env("PRINTER_HOST")
        self.printer_port = int(env("PRINTER_PORT", "8898"))
        self.serial = env("PRINTER_SERIAL")
        self.check_code = env("PRINTER_CHECK_CODE")
        self.spoolman = env("SPOOLMAN_URL").rstrip("/")
        self.slot_location = env("SLOT_LOCATION", "Creator 5 Pro / Slot {slot}")
        self.printstash = env("PRINTSTASH_URL").rstrip("/")
        self.ps_user = env("PRINTSTASH_USERNAME")
        self.ps_key = env("PRINTSTASH_API_KEY")
        self.ntfy_url = env("NTFY_URL")
        self.ntfy_token = env("NTFY_TOKEN")
        self.public_url = env("PUBLIC_URL").rstrip("/")
        self.action_token = env("ACTION_TOKEN")
        self.auto_assign = env_bool("AUTO_ASSIGN", True)
        self.slot_settle_s = float(env("SLOT_SETTLE_S", "60"))
        self.recheck_s = int(float(env("RECHECK_S", "300")))
        self.count_idle = env_bool("COUNT_IDLE_USE", True)
        self.poll_s = float(env("POLL_INTERVAL_S", "3"))
        self.data_dir = Path(env("DATA_DIR", "/data"))
        self.port = int(env("PORT", "8000"))

    def check(self) -> None:
        missing = [n for n, v in (("PRINTER_HOST", self.printer_host), ("PRINTER_SERIAL", self.serial),
                                  ("PRINTER_CHECK_CODE", self.check_code), ("SPOOLMAN_URL", self.spoolman)) if not v]
        if missing:
            raise SystemExit(f"Missing settings: {', '.join(missing)}")


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


class Printer:
    def __init__(self, cfg: Config, http: httpx.AsyncClient) -> None:
        self.url = f"http://{cfg.printer_host}:{cfg.printer_port}/detail"
        self.body = {"serialNumber": cfg.serial, "checkCode": cfg.check_code}
        self.http = http

    async def detail(self) -> dict[str, Any]:
        r = await self.http.post(self.url, json=self.body, timeout=5.0)
        r.raise_for_status()
        data = r.json()
        if data.get("code") not in (0, "0", None):
            raise RuntimeError(f"printer refused: {data.get('message') or data.get('code')}")
        detail = data.get("detail") or data.get("data") or {}
        if isinstance(detail, dict) and isinstance(detail.get("detail"), dict):
            detail = detail["detail"]
        return detail


class Spoolman:
    def __init__(self, cfg: Config, http: httpx.AsyncClient) -> None:
        self.base = f"{cfg.spoolman}/api/v1"
        self.template = cfg.slot_location
        self.http = http

    def location(self, slot: int) -> str:
        return self.template.format(slot=slot)

    async def spool_in_slot(self, slot: int) -> tuple[dict[str, Any] | None, str | None]:
        want = re.sub(r"\s+", " ", self.location(slot)).strip().lower()
        r = await self.http.get(f"{self.base}/spool", params={"location": self.location(slot)}, timeout=10)
        r.raise_for_status()
        spools = [s for s in r.json() if re.sub(r"\s+", " ", s.get("location") or "").strip().lower() == want]
        if not spools:
            return None, f"no spool has location '{self.location(slot)}'"
        if len(spools) > 1:
            names = ", ".join(f"#{s['id']}" for s in spools)
            return None, f"several spools have location '{self.location(slot)}' ({names})"
        return spools[0], None

    async def spools(self) -> list[dict[str, Any]]:
        r = await self.http.get(f"{self.base}/spool", timeout=10)
        r.raise_for_status()
        return r.json()

    async def spool(self, spool_id: int) -> dict[str, Any]:
        r = await self.http.get(f"{self.base}/spool/{spool_id}", timeout=10)
        r.raise_for_status()
        return r.json()

    async def set_location(self, spool_id: int, location: str | None) -> None:
        r = await self.http.patch(f"{self.base}/spool/{spool_id}", json={"location": location or None}, timeout=10)
        r.raise_for_status()

    @staticmethod
    def same_place(a: str | None, b: str | None) -> bool:
        return re.sub(r"\s+", " ", a or "").strip().lower() == re.sub(r"\s+", " ", b or "").strip().lower()

    async def use_length(self, spool_id: int, mm: float) -> dict[str, Any]:
        r = await self.http.put(f"{self.base}/spool/{spool_id}/use", json={"use_length": round(mm, 2)}, timeout=10)
        r.raise_for_status()
        return r.json()

    async def ok(self) -> bool:
        try:
            r = await self.http.get(f"{self.base}/health", timeout=5)
            return r.status_code == 200
        except httpx.HTTPError:
            return False


def _name_key(name: str) -> str:
    """Compare file names the way the printer and PrintStash may spell them."""
    base = re.split(r"[\\/]", name or "")[-1]
    base = re.sub(r"(\.gcode)?\.3mf$|\.(gcode|gx|g|bgcode)$", "", base, flags=re.IGNORECASE)
    return base.strip().lower()


class PrintStash:
    def __init__(self, cfg: Config, http: httpx.AsyncClient) -> None:
        self.base = f"{cfg.printstash}/api/v1"
        self.user, self.key = cfg.ps_user, cfg.ps_key
        self.http = http
        self.token: str | None = None
        self.token_at = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.base.startswith("http") and self.user and self.key)

    async def _auth(self, force: bool = False) -> dict[str, str]:
        if force or not self.token or time.monotonic() - self.token_at > 45 * 60:
            r = await self.http.post(f"{self.base}/auth/login", json={"username": self.user, "api_key": self.key}, timeout=10)
            r.raise_for_status()
            self.token, self.token_at = r.json()["access_token"], time.monotonic()
        return {"Authorization": f"Bearer {self.token}"}

    async def _get(self, path: str, **kw: Any) -> httpx.Response:
        for attempt in range(2):
            r = await self.http.get(f"{self.base}{path}", headers=await self._auth(attempt > 0), timeout=30, **kw)
            if r.status_code != 401:
                r.raise_for_status()
                return r
        r.raise_for_status()
        return r

    async def sliced_usage(self, job_file: str) -> tuple[SlicedUsage | None, str]:
        """The newest G-code revision named like the job, parsed; or why not."""
        key = _name_key(job_file)
        if not key:
            return None, "the printer gave no file name"
        hits = (await self._get("/search", params={"q": key, "limit": 20, "types[]": "model"})).json()
        best: tuple[str, int] | None = None
        for item in hits.get("items", []):
            model_id = (item.get("model") or {}).get("id") or item.get("subject_id")
            if not model_id:
                continue
            model = (await self._get(f"/models/{model_id}")).json()
            for f in model.get("files", []):
                if f.get("file_type") == "gcode" and _name_key(f.get("original_filename", "")) == key:
                    stamp = f.get("uploaded_at") or ""
                    if best is None or stamp > best[0]:
                        best = (stamp, int(f["id"]))
        if not best:
            return None, f"no G-code named '{key}' in PrintStash"
        data = (await self._get(f"/files/{best[1]}/download", follow_redirects=True)).content
        usage = parse_sliced_file(data)
        if not usage:
            return None, f"PrintStash file {best[1]} carries no filament usage"
        return usage, f"PrintStash file {best[1]}"


class Waiting(Exception):
    """A charge for a slot whose spool is not known yet: keep it until it is."""


class Ntfy:
    """Publishes as JSON, which keeps titles and button labels in UTF-8."""

    def __init__(self, cfg: Config, http: httpx.AsyncClient) -> None:
        self.token, self.http = cfg.ntfy_token, http
        url = cfg.ntfy_url.rstrip("/")
        self.base, _, self.topic = url.rpartition("/")

    async def send(self, title: str, body: str, tags: str = "printer",
                   actions: list[tuple[str, str]] | None = None) -> None:
        """actions: up to three (label, url) buttons; tapping one POSTs to url."""
        if not self.topic or not self.base.startswith("http"):
            return
        msg: dict[str, Any] = {"topic": self.topic, "title": title, "message": body, "tags": tags.split(",")}
        if actions:
            msg["actions"] = [{"action": "http", "label": label[:40], "url": url, "method": "POST", "clear": True}
                              for label, url in actions[:3]]
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            r = await self.http.post(self.base, json=msg, headers=headers, timeout=10)
            if r.status_code >= 400:
                log.warning("ntfy refused: %s %s", r.status_code, r.text[:200])
        except httpx.HTTPError as e:
            log.warning("ntfy failed: %s", e)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class Service:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.http = httpx.AsyncClient()
        self.printer = Printer(cfg, self.http)
        self.spoolman = Spoolman(cfg, self.http)
        self.printstash = PrintStash(cfg, self.http)
        self.ntfy = Ntfy(cfg, self.http)
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = cfg.data_dir / "state.json"
        self.history_path = cfg.data_dir / "history.jsonl"
        state = self._load()
        self.pending: list[dict[str, Any]] = state.pop("pending", [])
        # slot -> {key, load, state, spool_id, spool, prev_location, ask, charged_mm, problem}
        self.slots: dict[str, dict[str, Any]] = state.pop("slots", {})
        self.token = cfg.action_token or self._token()
        self._seen: dict[int, tuple[str, float]] = {}  # slot -> (changed key, first seen)
        self.tracker = Tracker(state.get("tracker"), count_idle=cfg.count_idle)
        self.online = False
        self.last_snapshot: Snapshot | None = None
        self.last_tick = time.time()
        self.last_error: str | None = None
        self._saved_at = 0.0

    # --- persistence -----------------------------------------------------

    def _token(self) -> str:
        path = self.cfg.data_dir / "action_token"
        try:
            return path.read_text().strip()
        except FileNotFoundError:
            token = secrets.token_urlsafe(24)
            path.write_text(token)
            return token

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text())
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            log.error("state.json is unreadable; starting fresh")
            return {}

    def save(self, force: bool = True) -> None:
        if not force and time.time() - self._saved_at < 30:
            return
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"tracker": self.tracker.state, "pending": self.pending, "slots": self.slots}))
        os.replace(tmp, self.state_path)
        self._saved_at = time.time()

    def record(self, entry: dict[str, Any]) -> None:
        with self.history_path.open("a") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # --- charging --------------------------------------------------------

    async def _charge(self, item: dict[str, Any], slot: int, mm: float) -> dict[str, Any]:
        """Deduct mm from the spool in slot, once per item and slot."""
        done = item.setdefault("charged", {})
        if str(slot) in done:
            return done[str(slot)]
        spool, problem = await self._spool_for(item, slot)
        if not spool:
            result = {"slot": slot, "mm": mm, "charged": False, "problem": problem}
        else:
            before = float(spool.get("used_weight") or 0)
            updated = await self.spoolman.use_length(int(spool["id"]), mm)
            result = {"slot": slot, "mm": mm, "charged": True, "spool_id": spool["id"],
                      "spool": (spool.get("filament") or {}).get("name"),
                      "grams": round(float(updated.get("used_weight") or 0) - before, 2),
                      "remaining_g": updated.get("remaining_weight")}
        done[str(slot)] = result
        cur = self.slots.get(str(slot))
        if result.get("charged") and cur and cur.get("spool_id") == result.get("spool_id"):
            cur["charged_mm"] = round(cur.get("charged_mm", 0.0) + mm, 2)
        self.save()
        return result

    async def _spool_for(self, item: dict[str, Any], slot: int) -> tuple[dict[str, Any] | None, str | None]:
        """The spool that was in the slot when the use happened."""
        sid = (item.get("slot_spools") or {}).get(str(slot))
        if sid:
            try:
                return await self.spoolman.spool(int(sid)), None
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 404:
                    raise
        load = (item.get("slot_loads") or {}).get(str(slot))
        if load:
            cur = self.slots.get(str(slot)) or {}
            if cur.get("load") == load:
                if cur.get("spool_id"):
                    return await self.spoolman.spool(int(cur["spool_id"])), None
                if self._unresolved(cur):
                    raise Waiting(f"slot {slot}: {cur.get('problem')}")
            return None, f"the spool that was loaded in slot {slot} was never identified"
        spool, problem = await self.spoolman.spool_in_slot(slot)
        if spool:
            return spool, None
        cur = self.slots.get(str(slot)) or {}
        if cur.get("spool_id"):  # assigned, but the location did not reach Spoolman
            return await self.spoolman.spool(int(cur["spool_id"])), None
        return None, cur.get("problem") or problem

    async def _settle_job(self, item: dict[str, Any]) -> None:
        job = item["data"]
        if "allocation" not in item:
            sliced, source = None, "PrintStash not configured"
            if self.printstash.enabled:
                try:
                    sliced, source = await self.printstash.sliced_usage(job.get("file", ""))
                except httpx.HTTPError as e:
                    source = f"PrintStash unavailable: {e}"
            alloc = allocate(job, sliced)
            item["allocation"] = {"method": alloc.method, "per_slot_mm": alloc.per_slot_mm,
                                  "unassigned_mm": alloc.unassigned_mm, "total_mm": alloc.total_mm,
                                  "notes": alloc.notes, "sliced_source": source}
            self.save()
        a = item["allocation"]
        results = [await self._charge(item, int(slot), mm) for slot, mm in a["per_slot_mm"].items() if mm > 0]
        entry = {"type": "job", "file": job.get("file"), "outcome": job.get("outcome"),
                 "started_at": job.get("started_at"), "ended_at": job.get("ended_at"),
                 "partial": job.get("partial"), **a, "charges": results}
        self.record(entry)
        await self._notify_job(entry)

    async def _settle_idle(self, item: dict[str, Any]) -> None:
        d = item["data"]
        if d["slot"] == "unattributed":
            self.record({"type": "idle", "unassigned_mm": round(d["metres"] * 1000, 2), "at": item["at"]})
            return
        result = await self._charge(item, int(d["slot"]), round(d["metres"] * 1000, 2))
        self.record({"type": "idle", "at": item["at"], "charges": [result]})
        if not result.get("charged"):
            await self.ntfy.send("Filament not charged", f"Slot {d['slot']}: {result.get('problem')}", "warning")

    async def _notify_job(self, e: dict[str, Any]) -> None:
        lines = []
        for c in e["charges"]:
            what = f"{c.get('grams')} g" if c.get("charged") else f"{c['mm'] / 1000:.2f} m not charged ({c.get('problem')})"
            lines.append(f"Slot {c['slot']} {c.get('spool') or ''}: {what}".replace("  ", " "))
        if e.get("unassigned_mm"):
            lines.append(f"Unassigned: {e['unassigned_mm'] / 1000:.2f} m")
        lines.append(f"Total {e['total_mm'] / 1000:.2f} m · {e['method']}")
        lines.extend(e.get("notes") or [])
        problem = any(not c.get("charged") for c in e["charges"]) or e.get("unassigned_mm")
        await self.ntfy.send(f"{e.get('file') or 'Print'}: {e.get('outcome')}", "\n".join(lines),
                             "warning" if problem else "white_check_mark")

    async def settle_pending(self) -> None:
        for item in list(self.pending):
            try:
                if item["kind"] == "job_finished":
                    await self._settle_job(item)
                else:
                    await self._settle_idle(item)
                self.pending.remove(item)
                self.save()
            except Waiting as w:  # its spool is not known yet; the user was told when the slot was loaded
                if item.get("waiting") != str(w):
                    item["waiting"] = str(w)
                    log.info("holding %s: %s", item["kind"], w)
                    self.save()
            except Exception as e:  # keep it pending and retry next round
                item["attempts"] = item.get("attempts", 0) + 1
                self.last_error = f"settling {item['kind']}: {e}"
                log.warning("could not settle %s (attempt %s): %s", item["kind"], item["attempts"], e)
                if item["attempts"] in (1, 10):
                    await self.ntfy.send("Filament not charged yet", f"{item['kind']}: {e}. Will retry.", "warning")
                self.save()

    # --- loop ------------------------------------------------------------

    def _queue(self, events: list[Event]) -> None:
        spools = {k: v["spool_id"] for k, v in self.slots.items() if v.get("spool_id")}
        loads = {k: v["load"] for k, v in self.slots.items() if v.get("load") and v.get("key", "").startswith("1|")}
        for ev in events:
            log.info("%s: %s", ev.kind, json.dumps(ev.data)[:300])
            self.pending.append({"kind": ev.kind, "data": ev.data, "at": time.time(),
                                 "slot_spools": dict(spools), "slot_loads": dict(loads)})
        if events:
            self.save()

    async def tick(self) -> None:
        self.last_tick = time.time()
        try:
            snap = snapshot_from_detail(await self.printer.detail())
        except Exception as e:
            if self.online:
                log.info("printer offline: %s", e)
            self.online = False
            self.last_error = f"printer: {e}"
            return
        if not self.online:
            log.info("printer online: status=%s file=%s", snap.status, snap.file)
        self.online = True
        self.last_snapshot = snap
        self._queue(self.tracker.observe(snap))
        if self.cfg.auto_assign:
            await self.sync_slots(snap)
        self.save(force=False)

    # --- which spool is in which slot ------------------------------------
    #
    # The printer reports every intermediate state while a load is set up on its
    # screen (a default material and colour first, then each change), so a slot
    # is matched only once its material and colour have stayed the same for
    # SLOT_SETTLE_S. Each load gets an id; use recorded while its spool is not
    # known yet waits for it instead of being lost.

    def _url(self, slot: int, spool_id: int) -> str:
        return f"{self.cfg.public_url}/assign?slot={slot}&spool={spool_id}&token={self.token}"

    async def sync_slots(self, snap: Snapshot) -> None:
        now = time.time()
        for sl in snap.slots:
            key = slot_key(sl.loaded, sl.material, sl.color)
            cur = self.slots.get(str(sl.id))
            try:
                if cur and cur.get("key") == key:
                    self._seen.pop(sl.id, None)
                    if self._unresolved(cur) and now - cur.get("checked_at", 0) >= self.cfg.recheck_s:
                        await self._match(sl.id, cur, only_if_changed=True)
                    continue
                seen = self._seen.get(sl.id)
                if seen is None:
                    # The slot is changing: use until now belongs to what was loaded.
                    self._queue(self.tracker.flush_slot(sl.id))
                if seen is None or seen[0] != key:
                    self._seen[sl.id] = (key, now)
                    if cur is not None:
                        continue
                elif now - seen[1] < self.cfg.slot_settle_s:
                    continue
                if cur and self.tracker.job:  # a swap mid-print is settled once the job is charged
                    continue
                await self._swap(sl, key, cur)
                self._seen.pop(sl.id, None)
            except httpx.HTTPError as e:
                self.last_error = f"slot {sl.id}: {e}"
                log.warning("could not follow slot %s: %s", sl.id, e)
                return  # Spoolman unreachable: try again next tick

    @staticmethod
    def _unresolved(entry: dict[str, Any]) -> bool:
        return entry.get("key", "").startswith("1|") and not entry.get("spool_id") \
            and entry.get("state") != "no_material"

    async def _swap(self, sl: Any, key: str, cur: dict[str, Any] | None) -> None:
        slot, here = sl.id, self.spoolman.location(sl.id)
        released = None
        if cur and cur.get("spool_id"):
            old = await self.spoolman.spool(int(cur["spool_id"]))
            await self._release(slot, cur, old)
            released = (describe(old), cur.get("prev_location"))
        now = time.time()
        entry: dict[str, Any] = {"key": key, "load": f"{slot}-{int(now * 1000)}", "spool_id": None,
                                 "charged_mm": 0.0, "material": sl.material, "color": sl.color, "since": now}
        self.slots[str(slot)] = entry
        log.info("slot %s: %s", slot, key)
        if not sl.loaded:
            entry["state"] = "empty"
            self.save()
            if released:
                back = f" It is back at '{released[1]}'." if released[1] else ""
                await self.ntfy.send(f"Slot {slot}: empty", f"{released[0]} was taken out.{back}", "thread")
            return
        if not sl.material or sl.material.upper() in ("?", "UNKNOWN"):
            entry.update(state="no_material", problem="material not set on the printer screen")
            self.save()
            await self.ntfy.send(f"⚠️ Slot {slot}: material not set",
                                 "A spool is loaded but no material is chosen on the printer screen. "
                                 "Choose material and colour there, then the spool is found.", "warning")
            return
        await self._match(slot, entry)

    async def _match(self, slot: int, entry: dict[str, Any], only_if_changed: bool = False) -> None:
        """Find the spool for a load; tell the user the outcome once."""
        here = self.spoolman.location(slot)
        spools = await self.spoolman.spools()
        taken = {int(v["spool_id"]) for k, v in self.slots.items() if k != str(slot) and v.get("spool_id")}
        here_ids = [int(s["id"]) for s in spools if self.spoolman.same_place(s.get("location"), here)]
        choice = choose(entry["material"], entry["color"], spools, taken=taken,
                        already_here=here_ids[0] if len(here_ids) == 1 else None)
        if choice.spool:
            sig = ["matched", int(choice.spool["id"])]
        elif choice.candidates:
            sig = ["ask"] + sorted(int(s["id"]) for s, _ in choice.candidates)
        else:
            sig = ["missing"]
        entry["checked_at"] = time.time()
        if only_if_changed and sig == entry.get("sig"):
            self.save(force=False)
            return
        entry["sig"] = sig
        loaded = f"{entry['material']}, {color_name(entry['color'])}"
        if choice.spool:
            await self._place(slot, entry, choice.spool)
            await self.ntfy.send(f"Slot {slot}: {describe(choice.spool)}",
                                 f"Loaded: {loaded}.\nSpoolman location set to '{here}'.", "white_check_mark")
        elif choice.candidates:
            entry.update(state="ask", ask=[int(s["id"]) for s, _ in choice.candidates],
                         problem="waiting for you to pick the spool")
            self.save()
            listed = "\n".join(f"• {describe(s)}" for s, _ in choice.candidates)
            if self.cfg.public_url:
                how, actions = "Tap the one that is loaded.", [
                    (f"#{s['id']} {(s.get('filament') or {}).get('name', '')}", self._url(slot, int(s["id"])))
                    for s, _ in choice.candidates]
            else:
                how, actions = f"Set the loaded one's location in Spoolman to '{here}'.", None
            await self.ntfy.send(f"❓ Slot {slot}: which spool?",
                                 f"Loaded: {loaded}.\nThese match the colour:\n{listed}\n{how}\n"
                                 f"Until then, filament used from slot {slot} is held, not lost.",
                                 "question", actions)
        else:
            entry.update(state="missing", problem=f"no {entry['material']} spool of this colour in Spoolman")
            self.save()
            await self.ntfy.send(f"⚠️ Slot {slot}: spool not in Spoolman",
                                 f"Loaded: {loaded}.\nNo {entry['material']} spool of this colour in Spoolman. "
                                 f"Add it with its colour and it is assigned within {self.cfg.recheck_s // 60} min. "
                                 f"If it is there with another colour, fix the colour or set its location to '{here}'.\n"
                                 f"Until then, filament used from slot {slot} is held, not lost.", "warning")

    async def _place(self, slot: int, entry: dict[str, Any], spool: dict[str, Any], by_user: bool = False) -> None:
        here = self.spoolman.location(slot)
        prev = spool.get("location")
        if not self.spoolman.same_place(prev, here):
            await self.spoolman.set_location(int(spool["id"]), here)
        entry.update(spool_id=int(spool["id"]), spool=describe(spool), state="matched", problem=None, ask=None,
                     sig=["matched", int(spool["id"])],
                     prev_location=None if self.spoolman.same_place(prev, here) else prev)
        self.save()
        log.info("slot %s: %s%s", slot, describe(spool), " (picked by the user)" if by_user else "")
        self.record({"type": "assign", "at": time.time(), "slot": slot, "spool_id": int(spool["id"]),
                     "spool": describe(spool), "by_user": by_user})

    async def _release(self, slot: int, cur: dict[str, Any], spool: dict[str, Any] | None) -> None:
        """The spool left the slot: give it back the place it came from."""
        if not spool:
            return
        if self.spoolman.same_place(spool.get("location"), self.spoolman.location(slot)):
            await self.spoolman.set_location(int(spool["id"]), cur.get("prev_location"))
        self.record({"type": "release", "at": time.time(), "slot": slot, "spool_id": int(spool["id"]),
                     "to": cur.get("prev_location")})

    async def correct(self, slot: int, spool_id: int) -> str:
        """A tap on a spool button: put that spool in the slot and move what was charged."""
        cur = self.slots.get(str(slot))
        if not cur or not cur.get("key", "").startswith("1|"):
            return f"slot {slot} has nothing loaded"
        if cur.get("spool_id") == spool_id:
            return f"slot {slot} already has {cur.get('spool')}"
        new = await self.spoolman.spool(spool_id)
        moved = float(cur.get("charged_mm") or 0.0)
        old_id = cur.get("spool_id")
        if old_id:
            old = await self.spoolman.spool(int(old_id))
            if moved > 0:
                await self.spoolman.use_length(int(old_id), -moved)
            await self._release(slot, cur, old)
        came_from = None
        for other_slot, v in self.slots.items():  # it cannot be in two slots
            if other_slot != str(slot) and v.get("spool_id") == spool_id:
                came_from = v.get("prev_location")
                v.update(spool_id=None, spool=None, state="missing", sig=None, checked_at=0,
                         problem=f"spool #{spool_id} was moved to slot {slot}")
        await self._place(slot, cur, new, by_user=True)
        if came_from is not None:
            cur["prev_location"] = came_from
        if moved > 0:
            await self.spoolman.use_length(spool_id, moved)
        cur["charged_mm"] = moved
        for item in self.pending:  # charges not made yet go to the right spool too
            if old_id and (item.get("slot_spools") or {}).get(str(slot)) == old_id:
                item["slot_spools"][str(slot)] = spool_id
        self.save()
        msg = f"Slot {slot}: {describe(new)}" + (f" ({moved / 1000:.2f} m moved from #{old_id})" if moved else "")
        await self.ntfy.send(f"Slot {slot}: {describe(new)}",
                             "Set by you." + (f" {moved / 1000:.2f} m already charged moved from #{old_id}." if moved else ""),
                             "white_check_mark")
        return msg

    async def run(self) -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        server = await asyncio.start_server(self._http, "0.0.0.0", self.cfg.port)
        log.info("watching %s:%s, Spoolman %s, PrintStash %s", self.cfg.printer_host, self.cfg.printer_port,
                 self.cfg.spoolman, self.cfg.printstash or "off")
        try:
            while not stop.is_set():
                await self.tick()
                if self.pending:
                    await self.settle_pending()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.cfg.poll_s)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.save()
            server.close()
            await self.http.aclose()

    # --- tiny HTTP endpoint ----------------------------------------------

    def status(self) -> dict[str, Any]:
        s = self.last_snapshot
        job = self.tracker.job
        history = []
        if self.history_path.exists():
            history = [json.loads(line) for line in self.history_path.read_text().splitlines()[-10:]]
        return {
            "printer_online": self.online,
            "printer_status": s.status if s else None,
            "file": s.file if s else None,
            "feeding_slot": self.tracker.state.get("last_slot"),
            "counter_m": self.tracker.state.get("last_cum_m"),
            "current_job": job and {"file": job["file"], "metres": job["metres"], "partial": job["partial"]},
            "idle_m": self.tracker.state.get("idle_m"),
            "slots": {k: {f: v.get(f) for f in ("state", "spool_id", "spool", "material", "color", "ask", "problem",
                                                  "charged_mm")} for k, v in sorted(self.slots.items())},
            "pending": len(self.pending),
            "held": [{"kind": i["kind"], "why": i["waiting"]} for i in self.pending if i.get("waiting")],
            "last_error": self.last_error,
            "recent": history,
        }

    async def _http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = (await asyncio.wait_for(reader.readline(), 5)).decode("latin-1")
            parts = line.split(" ")
            method, target = (parts[0], parts[1]) if len(parts) > 1 else ("GET", "/")
            url = urlsplit(target)
            path, query = url.path, {k: v[0] for k, v in parse_qs(url.query).items()}
            while (await asyncio.wait_for(reader.readline(), 5)) not in (b"\r\n", b"\n", b""):
                pass
            if path == "/assign":
                if method != "POST":
                    body, code = {"error": "use POST"}, 405
                elif not secrets.compare_digest(query.get("token", ""), self.token):
                    body, code = {"error": "bad token"}, 403
                else:
                    try:
                        body, code = {"ok": True, "result": await self.correct(int(query["slot"]), int(query["spool"]))}, 200
                    except (KeyError, ValueError):
                        body, code = {"error": "slot and spool are required"}, 400
                    except httpx.HTTPError as e:
                        body, code = {"error": f"Spoolman: {e}"}, 502
            elif path.startswith("/healthz"):
                alive = time.time() - self.last_tick < max(60, self.cfg.poll_s * 10)
                body, code = {"ok": alive, "printer_online": self.online}, 200 if alive else 503
            elif path.startswith("/status"):
                body, code = self.status(), 200
            else:
                body, code = {"error": "not found"}, 404
            data = json.dumps(body, ensure_ascii=False, indent=2).encode()
            reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
                      502: "Bad Gateway", 503: "Service Unavailable"}[code]
            writer.write(f"HTTP/1.1 {code} {reason}\r\nContent-Type: application/json; charset=utf-8\r\n"
                         f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = Config()
    cfg.check()
    asyncio.run(Service(cfg).run())


if __name__ == "__main__":
    main()
