"""Unit tests: matching what a slot reports to a spool in Spoolman."""

from assign import choose, color_name, family, slot_key


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
    assert c.spool["id"] == 2 and not c.candidates


def test_an_opened_spool_is_preferred():
    spools = [dict(s) for s in SPOOLS]
    spools[2] = dict(spools[2], used_weight=120.0, remaining_weight=980.0)
    assert choose("PETG", "#1A1A1A", spools).spool["id"] == 3


def test_a_spool_in_another_slot_is_not_taken_twice():
    assert choose("PETG", "#1A1A1A", SPOOLS, taken={2}).spool["id"] == 3


def test_clear_colour_match():
    c = choose("PLA", "#A8D8EA", SPOOLS)
    assert c.spool["id"] == 5 and not c.candidates


# What the printer really reported on 7 Nov 2026 while slot 1 was set up on its screen.

def test_printer_default_coral_is_not_guessed_as_rose_quartz():
    c = choose("PLA", "#FA6173", SPOOLS)
    assert c.spool is None and not c.candidates and "no PLA spool" in c.reason


def test_coral_is_not_guessed_as_a_red():
    spools = SPOOLS + [spool(12, 11, "HS PETG Metallic Red", "PETG", "B0182A"),
                       spool(16, 15, "PETG Red", "PETG", "C8102E")]
    c = choose("PETG", "#FA6173", spools)
    assert c.spool is None and not c.candidates


def test_white_and_transparent_are_asked_not_guessed():
    c = choose("PETG", "#FFFFFF", SPOOLS)
    assert c.spool is None and [s["id"] for s, _ in c.candidates] == [4, 8]


def test_printer_black_matches_the_black_spool():
    assert choose("PETG", "#1B1B1B", SPOOLS).spool["id"] == 2


def test_a_close_pink_matches_only_the_pink_it_is_close_to():
    c = choose("PLA", "#F2A7B5", SPOOLS)
    assert c.spool["id"] == 1 and not c.candidates


def test_material_keeps_families_apart():
    # A white PLA slot never picks the white PETG.
    c = choose("PLA", "#F2F2F2", SPOOLS)
    assert c.spool is None and not c.candidates


def test_unknown_material():
    c = choose("TPU", "#000000", SPOOLS)
    assert c.spool is None and "TPU" in c.reason


def test_a_location_already_set_wins():
    c = choose("PLA", "#FA6173", SPOOLS, already_here=6)
    assert c.spool["id"] == 6


def test_archived_spools_are_ignored():
    spools = [dict(s, archived=(s["id"] == 5)) for s in SPOOLS]
    assert choose("PLA", "#A8D8EA", spools).spool is None


def test_colour_names():
    assert color_name("#FFFFFF") == "white (#FFFFFF)"
    assert color_name("#FA6173").startswith("coral")
    assert color_name("1B1B1B") == "black (#1B1B1B)"
    assert color_name("") == "no colour"
