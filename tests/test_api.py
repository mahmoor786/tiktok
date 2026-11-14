"""Full flow through the web API with fake Gemini/Tavily behind it."""
import pytest
from fastapi.testclient import TestClient

from app import db, jobs, pipeline, work
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def signed(client):
    assert client.post("/api/login", json={"password": "test-password"}).status_code == 200
    return client


def test_everything_needs_a_login(client):
    assert client.get("/api/state").json() == {"signed_in": False}
    for path in ("/api/settings", "/api/packs", "/api/research", "/api/jobs/1"):
        assert client.get(path).status_code == 401
    assert client.post("/api/research", json={"category": "home"}).status_code == 401
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401


def test_page_and_static_files_load(client):
    page = client.get("/")
    assert page.status_code == 200 and "Content-Security-Policy" in page.headers
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_state_and_settings_hide_keys(signed):
    state = signed.get("/api/state").json()
    assert state["signed_in"] and state["setup"]["ready"] and len(state["categories"]) == 10
    saved = signed.post("/api/settings", json={"gemini_key": "AIza-secret-key-1234", "scenes": 99,
                                               "auto_time": "06:45", "auto_daily": True}).json()
    assert saved["gemini_key"] == {"saved": True, "ending": "1234"}
    assert "AIza" not in str(saved) and saved["scenes"] == "6" and saved["auto_time"] == "06:45"
    assert signed.post("/api/settings", json={"auto_time": "7am"}).status_code == 400
    signed.post("/api/settings", json={"auto_daily": False})


def test_connection_check_picks_models_and_reports_plainly(signed):
    report = signed.post("/api/settings/test").json()
    names = {c["name"]: c for c in report["checks"]}
    assert report["ready"] and names["Gemini key"]["ok"] and names["Tavily web search"]["ok"]
    assert names["Google Search through Gemini"]["ok"] is False and names["Google Search through Gemini"]["optional"]
    assert report["settings"]["model_main"] == "gemini-3.8-flash"
    assert report["settings"]["model_lite"] == "gemini-3.5-flash-lite"


def _finish(signed, job_id):
    jobs.wait_idle()
    return signed.get(f"/api/jobs/{job_id}").json()


def test_find_products_then_make_packs(signed):
    assert signed.post("/api/settings", json={"scenes": 1}).json()["scenes"] == "1"
    started = signed.post("/api/research", json={"category": "home", "note": "under £20"}).json()
    job = _finish(signed, started["job_id"])
    assert job["status"] == "done", job
    research = signed.get(f"/api/research/{started['research_id']}").json()
    names = [p["name"] for p in research["products"]]
    assert research["status"] == "ready" and len(names) == 10
    assert not any("no source" in n for n in names)          # the invented one was dropped
    used = {i for p in research["products"] for i in p["source_ids"]}
    assert used == {s["id"] for s in research["sources"]} and all(s["url"].startswith("https://") for s in research["sources"])

    assert signed.post(f"/api/research/{started['research_id']}/packs", json={"indexes": []}).status_code == 400
    picked = signed.post(f"/api/research/{started['research_id']}/packs", json={"indexes": [0, 3, 3, 77]}).json()
    assert len(picked["pack_ids"]) == 2
    assert _finish(signed, picked["job_id"])["status"] == "done"
    pack = signed.get(f"/api/packs/{picked['pack_ids'][0]}").json()
    body = pack["pack"]
    assert pack["status"] == "ready" and len(body["scenes"]) == 1 and len(body["hooks"]) == 2
    assert len(body["facts"]) == 2 and body["hashtags"][0] == "#ad" and body["hashtags"].count("#ad") == 1
    assert all("No on-screen text" in s["flow_prompt"] for s in body["scenes"])
    assert body["total_seconds"] == 8 and body["scenes"][0]["keep_seconds"] == 8 and pack["sources"]


def test_longer_videos_are_still_possible(signed):
    signed.post("/api/settings", json={"scenes": 4})
    pack_id = signed.get("/api/packs").json()["packs"][0]["id"]
    again = signed.post(f"/api/packs/{pack_id}/retry", json={}).json()
    _finish(signed, again["job_id"])
    body = signed.get(f"/api/packs/{pack_id}").json()["pack"]
    assert len(body["scenes"]) == 4 and body["total_seconds"] == 16
    signed.post("/api/settings", json={"scenes": 1})


def test_pack_from_a_link_reads_the_page(signed):
    assert signed.post("/api/packs/link", json={"url": "not a link"}).status_code == 400
    started = signed.post("/api/packs/link", json={"url": "https://shop.example.com/p/123"}).json()
    assert _finish(signed, started["job_id"])["status"] == "done"
    pack = signed.get(f"/api/packs/{started['pack_ids'][0]}").json()
    assert pack["status"] == "ready" and pack["product_name"] == "DEMO Curtain fairy lights 3m"
    assert any(s["url"] == "https://shop.example.com/p/123" for s in pack["sources"]) or pack["sources"]


def test_thin_data_is_flagged_not_dressed_up(signed):
    started = signed.post("/api/packs/link", json={"url": "https://shop.example.com/p/9", "name": "DEMO thin product"}).json()
    _finish(signed, started["job_id"])
    pack_id = started["pack_ids"][0]
    pack = signed.get(f"/api/packs/{pack_id}").json()
    assert pack["status"] == "insufficient" and "checked fact" in pack["error"]
    forced = signed.post(f"/api/packs/{pack_id}/retry", json={"force": True}).json()
    _finish(signed, forced["job_id"])
    pack = signed.get(f"/api/packs/{pack_id}").json()
    assert pack["status"] == "ready" and pack["pack"]["forced"] is True


def test_results_feed_the_next_research(signed):
    pack_id = signed.get("/api/packs").json()["packs"][-1]["id"]
    saved = signed.post(f"/api/packs/{pack_id}/results", json={"views": 12000, "sales": 4, "hook_used": "B", "notes": "evening post"}).json()
    assert saved["views"] == 12000 and saved["sales"] == 4 and saved["hook_used"] == "B"
    summary = pipeline.learning_summary()
    assert "12000 views" in summary and "4 sales" in summary and "hook B" in summary
    assert signed.post(f"/api/packs/{pack_id}/results", json={"views": -5}).status_code == 422


def test_delete_pack(signed):
    packs = signed.get("/api/packs").json()["packs"]
    assert signed.delete(f"/api/packs/{packs[0]['id']}").json() == {"ok": True}
    assert signed.get(f"/api/packs/{packs[0]['id']}").status_code == 404


def test_daily_search_runs_once_after_the_set_time(signed):
    db.set_settings({"auto_daily": "1", "auto_time": "00:00", "last_auto_date": ""})
    before = len(db.list_research())
    assert work.auto_tick() is True and work.auto_tick() is False
    jobs.wait_idle()
    latest = db.list_research()[0]
    assert len(db.list_research()) == before + 1 and latest["origin"] == "auto" and latest["status"] == "ready"
    db.set_settings({"auto_daily": "0"})


def test_logout(signed):
    signed.post("/api/logout")
    assert signed.get("/api/packs").status_code == 401
