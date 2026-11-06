"""Unit tests: taking the serial number and check code from a FlashForge WebUI folder."""

import json

from credentials import from_folder, resolve


def webui(tmp_path, printers, last=None, name="printer_details.json"):
    folder = tmp_path / "webui"
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(json.dumps({
        "lastUsedPrinterSerial": last,
        "printers": {p["SerialNumber"]: {"Name": "C5P", "ClientType": "new", **p} for p in printers},
    }))
    return folder


C5P = {"SerialNumber": "SNMQ123", "CheckCode": "abcd1234", "IPAddress": "192.168.0.81"}
OTHER = {"SerialNumber": "SNOTHER", "CheckCode": "zzzz", "IPAddress": "192.168.0.99"}


def test_one_saved_printer(tmp_path):
    p, why = from_folder(webui(tmp_path, [C5P]))
    assert p == {"serial": "SNMQ123", "check_code": "abcd1234", "host": "192.168.0.81"} and "only one" in why


def test_several_printers_matched_by_ip_then_last_used(tmp_path):
    folder = webui(tmp_path, [C5P, OTHER], last="SNOTHER")
    assert from_folder(folder, "192.168.0.81")[0]["serial"] == "SNMQ123"
    assert from_folder(folder, "")[0]["serial"] == "SNOTHER"


def test_ambiguous_or_missing(tmp_path):
    p, why = from_folder(webui(tmp_path, [C5P, OTHER]), "10.0.0.1")
    assert p is None and "2 printers" in why
    assert from_folder(tmp_path / "nope")[0] is None


def test_placeholder_check_code_is_ignored(tmp_path):
    assert from_folder(webui(tmp_path, [dict(C5P, CheckCode="123")]))[0] is None


def test_other_file_layouts_are_found(tmp_path):
    folder = tmp_path / "webui"
    folder.mkdir()
    (folder / "config.json").write_text(json.dumps({"printer": {"serialNumber": "SNMQ123", "checkCode": "abcd1234"}}))
    assert from_folder(folder)[0]["check_code"] == "abcd1234"


def test_resolve_saves_a_copy_and_prefers_settings(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    folder = webui(tmp_path, [C5P])
    assert resolve("", "", "192.168.0.81", data, [folder])[:3] == ("SNMQ123", "abcd1234", "192.168.0.81")
    assert json.loads((data / "printer.json").read_text())["serial"] == "SNMQ123"
    # The WebUI folder can go: the copy is enough.
    assert resolve("", "", "", data, [tmp_path / "gone"])[:2] == ("SNMQ123", "abcd1234")
    # Settings always win.
    assert resolve("SNX", "code", "h", data, [folder])[:2] == ("SNX", "code")
