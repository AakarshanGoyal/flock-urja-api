from collections import Counter, defaultdict

from app import hierarchy as hier
from app.portal.parsing import normalise_export_row

from .fake_portal import load


def _resolve():
    meters = [normalise_export_row(r) for r in load("export_sample.json")["data"]]
    return meters, hier.resolve(meters, load("dts.json"))


def test_blank_codes_are_filled_from_sibling_meters():
    meters, res = _resolve()
    # J100044 reports feeder {"name": "Feeder 5", "code": ""} but sits on DT-005.
    assert res.dt_paths["DT-005"]["feeder"] == {"code": "F-005", "name": "Feeder 5"}
    filled = [i for i in res.issues if i.kind == "blank_code_filled" and "J100044" in i.meter_ids]
    assert filled and filled[0].level == "feeder"


def test_dt_name_comes_from_register_with_alias():
    _, res = _resolve()
    assert res.dt_paths["DT-007"]["dt"]["name"] == "Sanganer DT 7"
    assert res.dt_aliases["DT-007"] == ["Old Malviya Nagar Xfmr"]


def test_feeder_follows_transformer_register():
    _, res = _resolve()
    # DT-029 is fed by F-001 per the register (not F-029).
    assert res.dt_paths["DT-029"]["feeder"]["code"] == "F-001"


def test_codes_reused_under_different_parents_stay_separate():
    meters, res = _resolve()
    by_dt = defaultdict(list)
    for m in meters:
        by_dt[m["dt_code"]].append(m)
    tree = hier.build_tree(res, by_dt, max_depth="subdivision")
    ids = []

    def walk(nodes):
        for n in nodes:
            ids.append(n["id"])
            walk(n.get("children", []))

    walk(tree)
    sd01 = [i for i in ids if i.endswith("/SD-01")]
    assert len(sd01) > 1, "SD-01 exists under several divisions and must not be merged"
    assert sum(n["meter_count"] for n in tree) == len(meters)
    assert any(i.kind == "code_reused_across_parents" for i in res.issues)


def test_every_meter_lands_on_exactly_one_dt_path():
    meters, res = _resolve()
    assert Counter(m["dt_code"] for m in meters).keys() <= res.dt_paths.keys()
    for path in res.dt_paths.values():
        assert all(path[level]["code"] for level in hier.HIERARCHY_LEVELS)
