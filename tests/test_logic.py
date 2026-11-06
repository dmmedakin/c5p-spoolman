"""Unit tests: G-code parsing, live measuring and splitting a job between slots."""

import io
import zipfile

from allocate import allocate
from gcode_usage import parse_gcode_text, parse_sliced_file
from tracker import Snapshot, Slot, Tracker

# The tail of a real OrcaSlicer 2.5 export for the Creator 5 Pro (two objects,
# filaments 1 and 3).
GCODE_TAIL = """G1 X10 Y10 E1
; CONFIG_BLOCK_START
; extruder_colour = #8EC9E6;#F2A7B5;#FA6173;#F1ECE2
; filament_colour = #8EC9E6;#F2A7B5;#FA6173;#F1ECE2
; filament_map = 1,1,3,1
; filament_type = PLA;PLA;PLA;PLA
; printer_model = Flashforge Creator 5 Pro
; CONFIG_BLOCK_END
; filament used [mm] = 698.29, 0.00, 2985.20, 0.00
; filament used [cm3] = 1.68, 0.00, 7.18, 0.00
; filament used [g] = 2.08, 0.00, 8.90, 0.00
"""

SLOTS = [
    Slot(1, "PLA", "#8EC9E6", True),
    Slot(2, "PLA", "#F2A7B5", True),
    Slot(3, "PLA", "#FA6173", True),
    Slot(4, "PLA", "#F1ECE2", True),
]


def snap(status, cum, slot, file="two-tool.gcode.3mf", duration=0.0, slots=SLOTS):
    return Snapshot(status=status, file=file, cumulative_m=cum, current_slot=slot,
                    duration_s=duration, slots=slots)


def test_parse_gcode():
    u = parse_gcode_text(GCODE_TAIL)
    assert [f.length_mm for f in u.filaments] == [698.29, 0.0, 2985.20, 0.0]
    assert [(f.index, f.color, f.material, f.head) for f in u.used] == [(1, "#8EC9E6", "PLA", 1), (3, "#FA6173", "PLA", 3)]


def test_parse_3mf_and_missing_block():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/plate_1.gcode", GCODE_TAIL)
        z.writestr("Metadata/slice_info.config", "<config/>")
    assert parse_sliced_file(buf.getvalue()).total_mm == 698.29 + 2985.20
    assert parse_gcode_text("G1 X1\n") is None
    assert parse_sliced_file(b"PK\x03\x04garbage") is None


def run(tracker, readings):
    events = []
    t = 1000.0
    for r in readings:
        t += 3
        events += tracker.observe(r, now=t)
    return events


def test_two_tool_job_measured_and_split_by_file():
    tr = Tracker()
    ev = run(tr, [
        snap("ready", 100.0, 0, file=""),
        snap("printing", 100.0, 1),
        snap("printing", 100.5, 1),   # purge + first layers on slot 1
        snap("printing", 101.2, 3),
        snap("printing", 102.5, 3),   # readings stay within a believable rate
        snap("printing", 103.9, 3),
        snap("completed", 104.1, 3),
    ])
    jobs = [e for e in ev if e.kind == "job_finished"]
    assert len(jobs) == 1 and jobs[0].data["outcome"] == "completed"
    job = jobs[0].data
    assert round(sum(job["metres"].values()), 3) == 4.1
    a = allocate(job, parse_gcode_text(GCODE_TAIL))
    assert "split by the sliced file" in a.method
    assert round(sum(a.per_slot_mm.values())) == 4100
    # 698 : 2985 of the measured 4.1 m
    assert round(a.per_slot_mm[1]) == round(4100 * 698.29 / 3683.49)
    assert set(a.per_slot_mm) == {1, 3}


def test_file_disagreeing_with_printer_falls_back_to_measured():
    tr = Tracker()
    ev = run(tr, [snap("printing", 50.0, 2), snap("printing", 51.0, 2), snap("completed", 51.0, 2)])
    a = allocate(ev[-1].data, parse_gcode_text(GCODE_TAIL))  # file says slots 1 and 3, printer fed slot 2
    assert a.method == "measured per slot" and a.per_slot_mm == {2: 1000.0}


def test_cancelled_job_uses_live_attribution():
    tr = Tracker()
    ev = run(tr, [snap("printing", 10.0, 1), snap("printing", 10.4, 1), snap("printing", 10.6, 3), snap("cancel", 10.6, 0)])
    job = ev[-1].data
    assert job["outcome"] == "cancelled"
    a = allocate(job, parse_gcode_text(GCODE_TAIL))
    assert a.method == "measured per slot"
    assert a.per_slot_mm == {1: 400.0, 3: 200.0}


def test_slot_zero_mid_change_uses_hot_head_then_last_slot():
    tr = Tracker()
    s = snap("printing", 5.0, 0)
    s.nozzle_targets = [0, 0, 220, 170]
    ev = run(tr, [snap("printing", 4.0, 2), s, snap("printing", 5.5, 0), snap("completed", 5.5, 0)])
    assert ev[-1].data["metres"] == {"3": 1.5}


def test_unattributed_spread_over_attributed_slots():
    job = {"outcome": "completed", "metres": {"1": 1.0, "unattributed": 0.5}, "slots_seen": []}
    a = allocate(job, None)
    assert a.per_slot_mm == {1: 1500.0}


def test_partial_job_counts_only_what_was_seen():
    tr = Tracker()
    ev = run(tr, [snap("printing", 200.0, 1, duration=3600), snap("printing", 200.8, 1, duration=3603), snap("completed", 200.8, 1)])
    job = ev[-1].data
    assert job["partial"] is True and job["metres"] == {"1": 0.8}
    assert any("before the service was watching" in n for n in allocate(job, None).notes)


def test_counter_glitch_is_not_charged():
    tr = Tracker()
    ev = run(tr, [snap("printing", 10.0, 1), snap("printing", 10.3, 1), snap("printing", 9000.0, 1),
                  snap("printing", 9000.2, 1), snap("printing", 3.0, 1), snap("completed", 3.1, 1)])
    assert round(ev[-1].data["metres"]["1"], 3) == 0.6


def test_idle_use_flushes_per_slot_and_before_a_job():
    tr = Tracker(idle_flush_m=0.2)
    ev = run(tr, [snap("ready", 1.0, 2, file=""), snap("ready", 1.25, 2, file=""), snap("ready", 1.3, 2, file=""),
                  snap("printing", 1.3, 2)])
    idle = [e.data for e in ev if e.kind == "idle_use"]
    assert idle == [{"slot": "2", "metres": 0.25}, {"slot": "2", "metres": 0.05}]


def test_idle_use_off():
    tr = Tracker(count_idle=False)
    assert run(tr, [snap("ready", 1.0, 2, file=""), snap("ready", 3.0, 2, file="")]) == []


def test_new_file_while_active_closes_previous_job():
    tr = Tracker()
    ev = run(tr, [snap("printing", 1.0, 1, file="a.3mf"), snap("printing", 1.5, 1, file="a.3mf"),
                  snap("printing", 1.6, 1, file="b.3mf"), snap("completed", 2.0, 1, file="b.3mf")])
    jobs = [e.data for e in ev if e.kind == "job_finished"]
    assert [(j["file"], j["outcome"]) for j in jobs] == [("a.3mf", "ended"), ("b.3mf", "completed")]


def test_state_survives_a_restart():
    tr = Tracker()
    run(tr, [snap("printing", 1.0, 1), snap("printing", 1.4, 1)])
    resumed = Tracker(state=__import__("json").loads(__import__("json").dumps(tr.state)))
    ev = resumed.observe(snap("completed", 1.7, 1), now=2000)
    assert round(ev[0].data["metres"]["1"], 3) == 0.7


def test_unknown_slot_single_loaded_and_unassigned():
    one = [{"id": 2, "loaded": True}, {"id": 1, "loaded": False}]
    a = allocate({"outcome": "completed", "metres": {"unattributed": 1.0}, "slots_seen": one}, None)
    assert a.per_slot_mm == {2: 1000.0}
    a = allocate({"outcome": "completed", "metres": {"unattributed": 1.0}, "slots_seen": [s.__dict__ for s in SLOTS]}, None)
    assert a.unassigned_mm == 1000.0 and not a.per_slot_mm
