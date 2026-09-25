"""HTTP 接口测试：真实 ASGI 调用，覆盖成功、报错、不可达、割、健康入口。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_protected_paths_true_optimum():
    r = client.post("/api/protected-paths", json={
        "source": "S", "target": "T",
        "segments": [
            {"id": "e1", "from": "S", "to": "A", "delay": 1},
            {"id": "e2", "from": "A", "to": "B", "delay": 1},
            {"id": "e3", "from": "B", "to": "T", "delay": 1},
            {"id": "e4", "from": "S", "to": "B", "delay": 5},
            {"id": "e5", "from": "A", "to": "T", "delay": 5},
        ],
    })
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["totalDelay"] == 12
    # 总延迟必须等于各路径延迟之和、各段延迟之和（可复算）
    assert sum(p["delay"] for p in data["paths"]) == 12
    assert sum(s["delay"] for p in data["paths"] for s in p["segments"]) == 12
    ids = [s["id"] for p in data["paths"] for s in p["segments"]]
    assert len(ids) == len(set(ids))


def test_validation_error_clears_with_located_field():
    r = client.post("/api/protected-paths", json={
        "source": "S", "target": "T",
        "segments": [
            {"id": "e1", "from": "S", "to": "T", "delay": -1},
        ],
    })
    assert r.status_code == 400
    errors = r.json()["errors"]
    assert any(e["loc"] == "segments[0].delay" for e in errors)


def test_duplicate_id_error():
    r = client.post("/api/protected-paths", json={
        "source": "S", "target": "T",
        "segments": [
            {"id": "x", "from": "S", "to": "A", "delay": 1},
            {"id": "x", "from": "A", "to": "T", "delay": 1},
        ],
    })
    assert r.status_code == 400
    assert any("id" in e["loc"] for e in r.json()["errors"])


def test_nonexistent_endpoint_error():
    r = client.post("/api/protected-paths", json={
        "source": "Z", "target": "T",
        "segments": [{"id": "e1", "from": "S", "to": "T", "delay": 1}],
    })
    assert r.status_code == 400
    assert any(e["loc"] == "source" for e in r.json()["errors"])


def test_unreachable_error():
    r = client.post("/api/protected-paths", json={
        "source": "S", "target": "T",
        "segments": [
            {"id": "e1", "from": "S", "to": "A", "delay": 1},
            {"id": "e2", "from": "B", "to": "T", "delay": 1},
        ],
    })
    assert r.status_code == 400
    assert any(e["loc"] == "target" and "不可达" in e["message"]
               for e in r.json()["errors"])


def test_insufficient_returns_cut_evidence():
    r = client.post("/api/protected-paths", json={
        "source": "S", "target": "T",
        "segments": [
            {"id": "e1", "from": "S", "to": "A", "delay": 2},
            {"id": "e2", "from": "S", "to": "B", "delay": 3},
            {"id": "e3", "from": "A", "to": "X", "delay": 4},
            {"id": "e4", "from": "B", "to": "X", "delay": 5},
            {"id": "e5", "from": "X", "to": "T", "delay": 1},
        ],
    })
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "insufficient"
    ss = set(data["cut"]["sourceSet"])
    assert "S" in ss and "T" not in ss
    assert {"S", "A", "B"} <= ss
    assert [e["id"] for e in data["cut"]["edges"]] == ["e5"]


def test_malformed_json_rejected():
    r = client.post("/api/protected-paths",
                    content=b"{not json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_index_page_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "束线保护" in r.text
