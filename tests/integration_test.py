"""End-to-end run against a real Spoolman and PrintStash with a simulated printer.

Not part of the image's checks: it needs Spoolman on :7912 and PrintStash on
:8000 running locally (see README, Develop). Run with
    python tests/integration_test.py <printstash-user> <printstash-api-key>
"""

import json
import os
import random
import struct
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPOOLMAN = "http://127.0.0.1:7912/api/v1"
PRINTSTASH = "http://127.0.0.1:8000/api/v1"
PS_USER, PS_KEY = sys.argv[1], sys.argv[2]

GCODE = open(os.path.join(HERE, "tests", "test_logic.py")).read().split('GCODE_TAIL = """')[1].split('"""')[0]

# ---- simulated printer -------------------------------------------------------

PRINTER = {
    "status": "ready", "printFileName": "", "cumulativeFilament": 1000.0, "printDuration": 0, "printProgress": 0,
    "nozzleTargetTemps": [0, 0, 0, 0],
    "matlStationInfo": {"currentSlot": 0, "slotCnt": 4, "slotInfos": [
        {"slotId": 1, "materialName": "PLA", "materialColor": "#8EC9E6", "hasFilament": True},
        {"slotId": 2, "materialName": "PLA", "materialColor": "#F2A7B5", "hasFilament": True},
        {"slotId": 3, "materialName": "PLA", "materialColor": "#FA6173", "hasFilament": True},
        {"slotId": 4, "materialName": "PETG", "materialColor": "#1A1A1A", "hasFilament": True},
    ]},
}
PRINTER_UP = {"up": True}
NTFY: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/detail":
            req = json.loads(body)
            if not PRINTER_UP["up"]:
                self.send_response(503); self.end_headers(); return
            if req != {"serialNumber": "SN123", "checkCode": "CC456"}:
                out = {"code": 1, "message": "bad check code"}
            else:
                out = {"code": 0, "message": "Success", "detail": PRINTER}
        else:  # ntfy topic
            NTFY.append({"title": self.headers.get("Title"), "body": body.decode(), "tags": self.headers.get("Tags")})
            out = {"id": "x"}
        data = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)


srv = HTTPServer(("127.0.0.1", 18898), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()


def set_printer(status=None, file=None, cum=None, slot=None, duration=None):
    if status is not None: PRINTER["status"] = status
    if file is not None: PRINTER["printFileName"] = file
    if cum is not None: PRINTER["cumulativeFilament"] = round(cum, 4)
    if slot is not None: PRINTER["matlStationInfo"]["currentSlot"] = slot
    if duration is not None: PRINTER["printDuration"] = duration


# ---- seed Spoolman and PrintStash ---------------------------------------------

c = httpx.Client(timeout=30)
vendor = c.post(f"{SPOOLMAN}/vendor", json={"name": "TestVendor"}).json()
pla = c.post(f"{SPOOLMAN}/filament", json={"name": "Test PLA", "vendor_id": vendor["id"], "material": "PLA", "density": 1.24, "diameter": 1.75, "weight": 1000}).json()
spools = {}
for slot in (1, 3):
    spools[slot] = c.post(f"{SPOOLMAN}/spool", json={"filament_id": pla["id"], "location": f"Creator 5 Pro / Slot {slot}"}).json()
spools["shelf"] = c.post(f"{SPOOLMAN}/spool", json={"filament_id": pla["id"], "location": "Study / Shelf"}).json()


def used_g(spool_id):
    return round(c.get(f"{SPOOLMAN}/spool/{spool_id}").json().get("used_weight") or 0, 3)


tok = c.post(f"{PRINTSTASH}/auth/login", json={"username": PS_USER, "api_key": PS_KEY}).json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}
def stl(sz):
    v=[(0,0,0),(sz,0,0),(sz,sz,0),(0,sz,0),(0,0,sz),(sz,0,sz),(sz,sz,sz),(0,sz,sz)]
    f=[(0,2,1),(0,3,2),(4,5,6),(4,6,7),(0,1,5),(0,5,4),(1,2,6),(1,6,5),(2,3,7),(2,7,6),(3,0,4),(3,4,7)]
    b=b"\0"*80+struct.pack("<I",len(f))
    for t in f:
        b+=struct.pack("<3f",0,0,0)+b"".join(struct.pack("<3f",*v[i]) for i in t)+b"\0\0"
    return b
job = c.post(f"{PRINTSTASH}/ingest/model", headers=H, files={"file": ("two-tool.stl", stl(13 + random.random()))}, data={"model_name": "two-tool"}).json()
for _ in range(30):
    j = c.get(f"{PRINTSTASH}/jobs/{job['job_id']}", headers=H).json()
    if j["state"] == "completed": break
    time.sleep(0.5)
model_id = j["model_id"]
c.post(f"{PRINTSTASH}/models/{model_id}/gcode-revisions", headers=H, files={"file": ("two-tool.gcode", GCODE.encode())})

# ---- run the service ------------------------------------------------------------

data_dir = tempfile.mkdtemp()
ENV = dict(os.environ, PRINTER_HOST="127.0.0.1", PRINTER_PORT="18898", PRINTER_SERIAL="SN123", PRINTER_CHECK_CODE="CC456",
           SPOOLMAN_URL="http://127.0.0.1:7912", PRINTSTASH_URL="http://127.0.0.1:8000", PRINTSTASH_USERNAME=PS_USER,
           PRINTSTASH_API_KEY=PS_KEY, NTFY_URL="http://127.0.0.1:18898/c5p", POLL_INTERVAL_S="0.2",
           DATA_DIR=data_dir, PORT="18899")


def start():
    return subprocess.Popen([sys.executable, os.path.join(HERE, "service.py")], env=ENV, cwd=HERE,
                            stdout=open(os.path.join(data_dir, "log.txt"), "a"), stderr=subprocess.STDOUT)


def step(**kw):
    set_printer(**kw)
    time.sleep(0.6)


failures = []


def check(name, cond, detail=""):
    print(("OK   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


proc = start()
time.sleep(1.5)
check("health endpoint", httpx.get("http://127.0.0.1:18899/healthz").json()["ok"])

# 1. Two-tool job, completed: measured 4.0 m split 698:2985 between slots 1 and 3.
before1, before3 = used_g(spools[1]["id"]), used_g(spools[3]["id"])
cum = 1000.0
step(status="printing", file="two-tool.gcode.3mf", cum=cum, slot=1, duration=1)
for slot, metres in ((1, 0.4), (1, 0.4), (3, 0.8), (3, 0.8), (3, 0.8), (1, 0.4), (3, 0.4)):
    cum += metres
    step(cum=cum, slot=slot)
step(status="completed")
time.sleep(1.5)
g1, g3 = used_g(spools[1]["id"]) - before1, used_g(spools[3]["id"]) - before3
grams_per_mm = 3.14159265 * (1.75 / 2) ** 2 * 1.24 / 1000
expect_total_g = 4000 * grams_per_mm
check("job 1 charged both slots", g1 > 0 and g3 > 0, f"{g1} {g3}")
check("job 1 total equals the measured 4.0 m", abs((g1 + g3) - expect_total_g) < 0.05, f"{g1 + g3} vs {expect_total_g}")
check("job 1 split follows the sliced file", abs(g1 / (g1 + g3) - 698.29 / 3683.49) < 0.01, f"{g1 / (g1 + g3)}")
status = httpx.get("http://127.0.0.1:18899/status").json()
last = status["recent"][-1]
check("history says sliced-file split", "split by the sliced file" in last["method"], last["method"])
check("ntfy got a job message", any("two-tool" in (n["title"] or "") for n in NTFY), str(NTFY))

# 2. Restart mid-job, then the printer drops off the network for a while.
before1 = used_g(spools[1]["id"])
step(status="printing", file="other-model.gcode.3mf", slot=1, duration=1)
cum += 0.5; step(cum=cum)
proc.terminate(); proc.wait()
cum += 0.3; set_printer(cum=cum)            # printing continues while the service is down
proc = start(); time.sleep(1.5)
PRINTER_UP["up"] = False; time.sleep(0.6)
cum += 0.2; set_printer(cum=cum)
PRINTER_UP["up"] = True
step(cum=cum)
step(status="completed")
time.sleep(1.5)
g = used_g(spools[1]["id"]) - before1
check("restart + offline: the whole 1.0 m charged once", abs(g - 1000 * grams_per_mm) < 0.05, f"{g} vs {1000 * grams_per_mm}")
last = httpx.get("http://127.0.0.1:18899/status").json()["recent"][-1]
check("no sliced file for other-model: measured per slot", last["method"] == "measured per slot", last["method"])

# 3. Idle use on slot 3, and on slot 4 which has no spool.
before3 = used_g(spools[3]["id"])
NTFY.clear()
step(status="ready", file="", slot=3)
cum += 0.25; step(cum=cum)
step(slot=4); cum += 0.3; step(cum=cum)
time.sleep(1)
check("idle use charged to slot 3", abs(used_g(spools[3]["id"]) - before3 - 250 * grams_per_mm) < 0.05)
check("idle use on an empty slot is reported", any("Slot 4" in n["body"] and "no spool" in n["body"] for n in NTFY), str(NTFY))
check("spool on the shelf untouched", used_g(spools["shelf"]["id"]) == 0)

# 4. Cancelled job: live attribution, no sliced-file split.
before1, before3 = used_g(spools[1]["id"]), used_g(spools[3]["id"])
step(status="printing", file="two-tool.gcode.3mf", slot=1, duration=1)
cum += 0.6; step(cum=cum)
cum += 0.3; step(cum=cum, slot=3)
step(status="cancel")
time.sleep(1.5)
d1, d3 = used_g(spools[1]["id"]) - before1, used_g(spools[3]["id"]) - before3
check("cancelled job charged by live attribution", abs(d1 - 600 * grams_per_mm) < 0.05 and abs(d3 - 300 * grams_per_mm) < 0.05, f"{d1} {d3}")

# 5. Wrong check code: the service reports it and charges nothing.
proc.terminate(); proc.wait()
ENV["PRINTER_CHECK_CODE"] = "wrong"
proc = start(); time.sleep(1.5)
st = httpx.get("http://127.0.0.1:18899/status").json()
check("wrong check code is reported", "bad check code" in (st["last_error"] or ""), st["last_error"])
proc.terminate(); proc.wait()

print("\nservice log tail:\n" + "".join(open(os.path.join(data_dir, "log.txt")).readlines()[-12:]))
print("FAILURES:", failures or "none")
srv.shutdown()
sys.exit(1 if failures else 0)
