# c5p-spoolman

Charges [Spoolman](https://github.com/Donkie/Spoolman) spools with the filament a
FlashForge Creator 5 Pro really uses, whichever way the job was started: Orca
Slicer, FlashForge Studio, a USB stick or the printer's screen.

It needs no slicer plugin and no upload through a web UI. It watches the printer.

## How it works

1. **Measure.** Every few seconds it reads the printer's local API (`POST /detail`
   on port 8898). `cumulativeFilament` is the printer's own lifetime counter in
   metres, so the counter's growth during a job is what the job really used. It
   counts cancelled, failed and paused-and-resumed jobs too.
2. **Attribute.** Each increase goes to the slot that is feeding at that moment:
   `matlStationInfo.currentSlot`. If that reads 0, it goes to the only head heated
   to a print temperature. If neither tells, it goes to the last slot it knew.
3. **Split by the sliced file.** When a job completes and its sliced file is in
   [PrintStash](https://github.com/xiao-villamor/PrintStash), the per-filament
   lengths in the G-code footer (`; filament used [mm] = …`) give the exact ratio
   between slots. The measured total is split in that ratio. It is used only if
   the file's filaments map onto the slots that really fed: matched by material
   and colour, or else by `filament_map`. Otherwise the live attribution stands.
4. **Charge.** For each slot it finds the spool whose Spoolman *location* is
   `Creator 5 Pro / Slot N` and calls `PUT /api/v1/spool/{id}/use` with the
   length. Spoolman converts length to grams with the filament's density and
   diameter.
5. **Between jobs** (loading, unloading, purges), counter growth is charged to the
   slot that fed, if `COUNT_IDLE_USE` is on.

The total always comes from the printer's counter. The slicer estimate never
replaces it; it only decides the split.

### Reliability

- State lives in `/data/state.json`, written atomically. After a restart or an
  outage the job continues from its starting counter, so nothing is lost and
  nothing is charged twice.
- Charges wait in a queue until Spoolman accepts them. Each slot of a job is
  marked once it is charged, so a retry never charges it again.
- A counter jump faster than the filament can physically move is rejected as a
  glitch. A counter that goes backwards (reset or swap) starts a new baseline.
- Every job and idle charge is appended to `/data/history.jsonl`.
- Problems such as an empty slot with no spool, two spools in one slot, or
  Spoolman being down are written to the log, shown in `/status` and, with
  ntfy set up, sent to your phone.

## Set up

1. **Find the serial number and check code.** Both are on the printer's
   network settings screen (the same values FlashForge apps ask for in LAN mode).
2. **Set the spool locations in Spoolman.** For each loaded spool, set
   *Location* to `Creator 5 Pro / Slot 1` … `Slot 4`. Spools elsewhere are never
   touched. When you swap a spool, change its location.
3. **Add the service** to your compose file:

```yaml
  c5p-spoolman:
    image: ghcr.io/dmmedakin/c5p-spoolman:latest
    container_name: c5p-spoolman
    restart: unless-stopped
    environment:
      PRINTER_HOST: 192.168.0.81
      PRINTER_SERIAL: "<serial>"
      PRINTER_CHECK_CODE: "<check code>"
      SPOOLMAN_URL: http://spoolman:8000
      PRINTSTASH_URL: http://printstash:3000      # optional
      PRINTSTASH_USERNAME: "<user>"
      PRINTSTASH_API_KEY: "<psk_...>"
      NTFY_URL: https://ntfy.example.com/printer  # optional
      DRY_RUN: "true"                             # first print: log only
    volumes:
      - /srv/appdata/3dprint/c5p-spoolman:/data
    ports:
      - "3065:8000"
```

Run the first print with `DRY_RUN: "true"`. Then compare `/status` with the
printer's own job summary and remove the line.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `PRINTER_HOST` | — | printer IP or name (required) |
| `PRINTER_SERIAL` | — | serial number (required) |
| `PRINTER_CHECK_CODE` | — | check code (required) |
| `PRINTER_PORT` | `8898` | local API port |
| `SPOOLMAN_URL` | — | e.g. `http://spoolman:8000` (required) |
| `SLOT_LOCATION` | `Creator 5 Pro / Slot {slot}` | Spoolman location of slot N |
| `PRINTSTASH_URL` | — | where sliced files are; enables the exact split |
| `PRINTSTASH_USERNAME`, `PRINTSTASH_API_KEY` | — | with `PRINTSTASH_URL` |
| `NTFY_URL`, `NTFY_TOKEN` | — | a phone message per job and per problem |
| `COUNT_IDLE_USE` | `true` | also charge loading, unloading and purges |
| `POLL_INTERVAL_S` | `3` | seconds between reads |
| `DRY_RUN` | `false` | log what would be charged, change nothing |
| `DATA_DIR` | `/data` | state and history |
| `PORT` | `8000` | `/healthz` and `/status` |

## Endpoints

- `GET /healthz`: 200 while the poll loop runs. A printer that is off is normal
  and does not make it unhealthy.
- `GET /status`: printer state, the current job and its per-slot metres so far,
  pending charges, the last error and the last 10 history entries.

## Still to confirm on a real printer

The logic is tested against a simulated printer, a real Spoolman and a real
PrintStash. These points about the printer itself are not confirmed yet:

- how precise `cumulativeFilament` is, and whether it counts purges;
- whether `currentSlot` follows the active head during a toolchange print, and
  whether slot N feeds head N;
- whether polling port 8898 alongside Orca or FlashForge Studio causes any issue;
- how `printFileName` relates to the name stored in PrintStash. Matching uses the
  file stem, so `part.gcode.3mf` and `part.gcode` both match `part`.

## Develop

```sh
pip install -r requirements.txt pytest
PYTHONPATH=. pytest -q tests/test_logic.py
```

`tests/integration_test.py` runs the whole service against a local Spoolman on
:7912 and PrintStash on :8000 with a simulated printer:

```sh
python tests/integration_test.py <printstash-user> <printstash-api-key>
```

## License

MIT
