from app.portal import parsing

from .fake_portal import load


def test_devalue_resolves_shared_references():
    assert parsing.devalue_unflatten([{"a": 1, "b": 1}, "x"]) == {"a": "x", "b": "x"}
    assert parsing.devalue_unflatten([[1, -1], 5]) == [5, None]


def test_legacy_meter_detail_is_normalised():
    page = parsing.parse_sveltekit_data(load("meter_page_legacy.json"))
    detail = parsing.normalise_meter_detail(page.data)
    assert detail["build"] == "legacy"
    assert detail["serial_no"] == "SE33962"
    assert detail["install_type"] == "Whole Current"
    assert detail["hierarchy"]["substation"] == {"name": "Substation 1", "code": "SS-01"}
    assert detail["hierarchy"]["dt"] == {"name": "Malviya Nagar DT 1", "code": "DT-001"}


def test_v2_meter_detail_parses_embedded_json_string():
    page = parsing.parse_sveltekit_data(load("meter_page_v2.json"))
    detail = parsing.normalise_meter_detail(page.data)
    assert detail["build"] == "v2"
    assert detail["meter_id"] == "J100004"
    assert detail["install_status"] == "Faulty"
    assert detail["phase_type"] == "single"


def test_missing_meter_is_a_200_with_error_node():
    page = parsing.parse_sveltekit_data(load("meter_page_not_found.json"))
    assert page.kind == "error" and page.error_status == 404


def test_expired_session_redirect_is_detected():
    page = parsing.parse_sveltekit_data({"type": "redirect", "location": "/login"})
    assert page.kind == "redirect" and page.redirect_to == "/login"


def test_hierarchy_label_with_blank_code():
    assert parsing.parse_hierarchy_label("Circle 6 ()") == {"name": "Circle 6", "code": ""}
    assert parsing.parse_hierarchy_label(" (C-01)") == {"name": "", "code": "C-01"}
    assert parsing.parse_hierarchy_label(None) == {"name": "", "code": ""}


def test_export_row_blank_strings_become_none_and_geo_floats():
    row = load("export_sample.json")["data"][0]
    m = parsing.normalise_export_row(row)
    assert isinstance(m["latitude"], float)
    assert m["hierarchy"]["dt"]["code"] == "DT-001"


def test_energy_duplicate_timestamp_keeps_complete_row():
    series = parsing.normalise_energy(load("energy_daily_duplicate.json")["data"])
    assert series.resolution == "daily"
    assert len(series.readings) == 8
    last = series.readings[-1]
    assert last["kwh_register"] == 12732.34 and last["voltage_r"] == 229
    assert any(i.startswith("duplicate_timestamp") for i in series.issues)


def test_energy_half_hourly_deltas_and_iso_timestamps():
    series = parsing.normalise_energy(load("energy_half_hourly_head.json")["data"])
    assert series.resolution == "30min" and series.interval_minutes == 30
    first, second = series.readings[0], series.readings[1]
    assert first["timestamp"] == "2026-06-23T23:30:00+05:30"
    assert first["kwh_consumed"] is None
    assert second["kwh_consumed"] == round(48439.16 - 48438.74, 4)


def test_energy_register_going_backwards_is_flagged_not_negative():
    rows = [
        {"timestamp": "01/07/2026 00:00", "kwh": "100", "kvah": "110", "voltR": "230"},
        {"timestamp": "02/07/2026 00:00", "kwh": "90", "kvah": "120", "voltR": "230"},
    ]
    series = parsing.normalise_energy(rows)
    assert series.readings[1]["kwh_consumed"] is None
    assert any(i.startswith("register_went_backwards") for i in series.issues)
