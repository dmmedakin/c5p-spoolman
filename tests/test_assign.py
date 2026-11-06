"""Unit tests: matching what a slot reports to a spool in Spoolman."""

from assign import choose, family, slot_key


def spool(sid, fid, name, material, color, used=0.0, remaining=1000.0, location="Study / Wooden sideboard"):
    return {"id": sid, "used_weight": used, "remaining_weight": remaining, "location": location,
            "filament": {"id": fid, "name": name, "material": material, "color_hex": color,
                         "vendor": {"name": "V"}}}


# A slice of a real Spoolman: two identical black PETG, near pinks, near whites.
SPOOLS = [
    spool(1, 1, "HS PLA+ Light Pink", "PLA+", "E1A6AD"),
    spool(2, 2, "Matte PETG Black", "PETG", "1A1A1A"),
    spool(3, 2, "Matte PETG Black", "PETG", "1A1A1A"),
    spool(4, 3, "Matte PETG White", "PETG", "F2F2F2"),
    spool(5, 4, "HS PLA+ Ice Blue", "PLA+", "A8D8EA"),
    spool(6, 5, "HS PLA Multicolor Rose Quartz", "PLA", "C98BA8"),
    spool(8, 7, "HS PETG Transparent", "PETG", "E8EEF0"),
    spool(17, 16, "CR-Wood White Pine", "PLA Wood", "D9C4A0"),
]


def test_families():
    assert family("PLA+") == family("HS PLA") == family("PLA Wood") == family("pla-cf") == "PLA"
    assert family("PETG") == family("HS PETG") == "PETG"
    assert family("PET") == "PET" and family("PCTG") == "PCTG"
    assert slot_key(False, "PLA", "#FFFFFF") == slot_key(False, "", "") == "0||"
    assert slot_key(True, "PLA+", "#a8d8ea") == slot_key(True, "PLA", "#A8D8EA")


def test_identical_spools_need_no_question():
    c = choose("PETG", "#1A1A1A", SPOOLS)
    assert c.spool["id"] == 2 and not c.doubtful


def test_an_opened_spool_is_preferred():
    spools = [dict(s) for s in SPOOLS]
    spools[2] = dict(spools[2], used_weight=120.0, remaining_weight=980.0)
    assert choose("PETG", "#1A1A1A", spools).spool["id"] == 3


def test_a_spool_in_another_slot_is_not_taken_twice():
    assert choose("PETG", "#1A1A1A", SPOOLS, taken={2}).spool["id"] == 3


def test_clear_colour_match():
    c = choose("PLA", "#A8D8EA", SPOOLS)
    assert c.spool["id"] == 5 and not c.doubtful


def test_near_pinks_are_doubtful_with_the_other_offered():
    c = choose("PLA", "#F2A7B5", SPOOLS)
    assert c.spool["id"] == 1 and c.doubtful
    assert [s["id"] for s, _ in c.alternatives] == [6]


def test_white_and_transparent_are_doubtful():
    c = choose("PETG", "#FFFFFF", SPOOLS)
    assert c.spool["id"] == 4 and c.doubtful and [s["id"] for s, _ in c.alternatives] == [8]


def test_two_reds_are_doubtful():
    spools = SPOOLS + [spool(12, 11, "HS PETG Metallic Red", "PETG", "B0182A"),
                       spool(16, 15, "PETG Red", "PETG", "C8102E")]
    c = choose("PETG", "#FF0000", spools)
    assert c.spool["id"] == 16 and c.doubtful and [s["id"] for s, _ in c.alternatives] == [12]


def test_material_keeps_families_apart():
    # A white PLA slot never picks the white PETG.
    c = choose("PLA", "#F2F2F2", SPOOLS)
    assert c.spool is None or (c.spool.get("filament") or {}).get("material", "").startswith("PLA")


def test_nothing_close_enough():
    c = choose("PLA", "#00FF00", SPOOLS)
    assert c.spool is None and "close" in c.reason


def test_unknown_material():
    c = choose("TPU", "#000000", SPOOLS)
    assert c.spool is None and "TPU" in c.reason


def test_a_location_already_set_wins():
    c = choose("PLA", "#F2A7B5", SPOOLS, already_here=6)
    assert c.spool["id"] == 6 and not c.doubtful


def test_archived_spools_are_ignored():
    spools = [dict(s, archived=(s["id"] == 5)) for s in SPOOLS]
    assert (choose("PLA", "#A8D8EA", spools).spool or {}).get("id") != 5
