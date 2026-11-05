def test_list_meters_with_filters(api):
    r = api.get("/meters", params={"status": "Faulty", "page_size": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["pagination"]["page_size"] == 5
    assert all(m["install_status"] == "Faulty" for m in body["data"])
    assert body["index"]["source"] == "export"


def test_meter_detail_has_repaired_hierarchy_and_flags(api):
    m = api.get("/meters/j100044").json()  # lower-case id accepted
    assert m["meter_id"] == "J100044"
    assert m["hierarchy"]["feeder"]["code"] == "F-005"
    assert m["portal_hierarchy"]["feeder"]["code"] == ""
    assert m["quality_flags"][0]["kind"] == "blank_code_filled"


def test_unknown_meter_is_404(api):
    r = api.get("/meters/J999999")
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_near_query_sorted_by_distance(api):
    m = api.get("/meters/J100000").json()
    loc = m["location"]
    r = api.get("/meters", params={"near_lat": loc["latitude"], "near_lng": loc["longitude"], "radius_km": 5}).json()
    assert r["data"][0]["meter_id"] == "J100000"
    dists = [x["distance_km"] for x in r["data"]]
    assert dists == sorted(dists) and all(d <= 5 for d in dists)


def test_consumption_raw_and_daily(api):
    raw = api.get("/meters/J100089/consumption").json()
    assert raw["resolution"] == "daily" and len(raw["readings"]) == 8
    assert any(i.startswith("duplicate_timestamp") for i in raw["issues"])
    daily = api.get("/meters/J100089/consumption", params={"granularity": "daily"}).json()
    assert daily["daily"][0]["date"] == "2026-06-23"
    assert round(sum(d["kwh"] for d in daily["daily"]), 2) == round(12732.34 - 12678.83, 2)


def test_consumption_flags_decommissioned_meter_that_still_reports(api):
    body = api.get("/meters/J100000/consumption").json()
    assert body["install_status"] == "Decommissioned"
    assert "decommissioned_meter_still_reporting_consumption" in body["issues"]


def test_consumption_is_cached(api, portal):
    api.get("/meters/J100001/consumption")
    api.get("/meters/J100001/consumption")
    assert portal.calls.count("GET /portal/meters/J100001/energy") == 1


def test_stale_consumption_served_when_portal_down(api, portal):
    api.get("/meters/J100001/consumption")
    portal.down = True
    r = api.get("/meters/J100001/consumption", params={"refresh": True})
    assert r.status_code == 200 and r.headers.get("X-Data-Stale") == "true"


def test_live_meter_v2(api):
    r = api.get("/meters/J100004/live").json()
    assert r["build"] == "v2" and r["install_type"] == "CT Operated"
    assert r["differences_from_index"] == {}


def test_transformer_detail(api):
    t = api.get("/transformers/DT-007").json()
    assert t["name"] == "Sanganer DT 7"
    assert "Old Malviya Nagar Xfmr" in t["aliases"]
    assert t["meter_count"] == len(t["meters"])


def test_hierarchy_tree_and_issues(api):
    tree = api.get("/hierarchy", params={"depth": "zone"}).json()
    assert {n["code"] for n in tree} == {"Z-01", "Z-02", "Z-03"}
    issues = api.get("/hierarchy/issues", params={"kind": "dt_name_mismatch"}).json()
    assert issues[0]["dt_code"] == "DT-007"


def test_falls_back_to_search_crawl_when_export_fails(api, portal):
    portal.export_broken = True
    meta = api.post("/sync").json()
    assert meta["source"] == "search_crawl"
    assert meta["meter_count"] == len(portal.export)


def test_portal_down_with_no_cache_is_503(tmp_path, portal):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.portal.client import PortalClient

    from .conftest import _no_sleep, make_settings

    s = make_settings(tmp_path)
    portal.down = True
    app = create_app(s, client=PortalClient(s, transport=portal.transport(), sleep=_no_sleep), warm_index=False)
    with TestClient(app) as c:
        r = c.get("/meters")
    assert r.status_code == 503 and r.json()["error"] == "portal_unavailable"


def test_stats(api):
    s = api.get("/stats").json()
    assert s["meters"] == sum(s["by_status"].values())
    assert s["meters_with_quality_flags"] > 0


def test_list_meters_quality_flags_filter(api):
    r_flags = api.get("/meters", params={"has_quality_flags": True}).json()
    assert r_flags["pagination"]["total"] > 0
    # Every returned meter has quality flags
    for item in r_flags["data"]:
        meter = api.get(f"/meters/{item['meter_id']}").json()
        assert len(meter["quality_flags"]) > 0

    r_clean = api.get("/meters", params={"has_quality_flags": False}).json()
    assert r_clean["pagination"]["total"] > 0
    for item in r_clean["data"][:5]:
        meter = api.get(f"/meters/{item['meter_id']}").json()
        assert len(meter["quality_flags"]) == 0
