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


AUDIT_CASE = {
    "source": "S", "target": "T",
    "segments": [
        {"id": "e1", "from": "S", "to": "A", "delay": 0},
        {"id": "x", "from": "A", "to": "B", "delay": 2},
        {"id": "e2", "from": "B", "to": "T", "delay": 0},
        {"id": "e3", "from": "S", "to": "C", "delay": 0},
        {"id": "e4", "from": "C", "to": "T", "delay": 0},
        {"id": "e5", "from": "A", "to": "C", "delay": 1},
        {"id": "e6", "from": "C", "to": "B", "delay": 1},
    ],
}


def _check_witness_payload(seg_entry, baseline_total):
    """见证载荷复核：两条完整链路、边互不重复、不含被审计段、总延迟相同。"""
    witness = seg_entry["witness"]
    assert isinstance(witness, list) and len(witness) == 2
    used = []
    for path in witness:
        chain = path["segments"]
        assert chain[0]["from"] == "S" and chain[-1]["to"] == "T"
        for a, b in zip(chain, chain[1:]):
            assert a["to"] == b["from"]
        assert path["delay"] == sum(s["delay"] for s in chain)
        used.extend(s["id"] for s in chain)
    assert seg_entry["id"] not in used
    assert len(used) == len(set(used))
    assert sum(p["delay"] for p in witness) == baseline_total


def test_segment_audit_classifications_and_witness():
    r = client.post("/api/segment-audit", json=AUDIT_CASE)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["totalDelay"] == 2
    # 基准方案并列展示所需的延迟与边集
    assert len(data["baseline"]) == 2
    assert sum(p["delay"] for p in data["baseline"]) == data["totalDelay"]

    entries = {s["id"]: s for s in data["segments"]}
    assert set(entries) == {f"e{i}" for i in range(1, 7)} | {"x"}
    assert entries["x"]["classification"] == "replaceable"
    assert entries["x"]["usedInBaseline"] is True
    for sid in ("e1", "e2", "e3", "e4"):
        assert entries[sid]["classification"] == "mandatory"
        assert entries[sid]["witness"] is None  # 必经段不得伪造见证
    for sid in ("e5", "e6"):
        assert entries[sid]["classification"] == "unused"
        assert entries[sid]["usedInBaseline"] is False
        assert entries[sid]["witness"] is None
    _check_witness_payload(entries["x"], data["totalDelay"])


def test_segment_audit_single_segment_consistent_with_full_table():
    """按段复算：单段结论必须与整表审计完全一致。"""
    full = client.post("/api/segment-audit", json=AUDIT_CASE).json()
    for sid in ("x", "e1", "e5"):
        payload = dict(AUDIT_CASE, segmentId=sid)
        r = client.post("/api/segment-audit", json=payload)
        assert r.status_code == 200
        one = r.json()["segments"]
        assert len(one) == 1
        expected = next(s for s in full["segments"] if s["id"] == sid)
        assert one[0] == expected


def test_segment_audit_deterministic_across_requests():
    """重复查看同一段：归类与替代见证必须保持一致。"""
    r1 = client.post("/api/segment-audit", json=AUDIT_CASE).json()
    r2 = client.post("/api/segment-audit", json=AUDIT_CASE).json()
    assert r1 == r2


def test_segment_audit_unknown_segment_id():
    r = client.post("/api/segment-audit", json=dict(AUDIT_CASE, segmentId="nope"))
    assert r.status_code == 400
    assert any(e["loc"] == "segmentId" for e in r.json()["errors"])


def test_segment_audit_requires_solvable_dual_path():
    insufficient = {
        "source": "S", "target": "T",
        "segments": [{"id": "a", "from": "S", "to": "T", "delay": 1}],
    }
    r = client.post("/api/segment-audit", json=insufficient)
    assert r.status_code == 400
    unreachable = {
        "source": "S", "target": "T",
        "segments": [
            {"id": "a", "from": "S", "to": "A", "delay": 1},
            {"id": "b", "from": "B", "to": "T", "delay": 1},
        ],
    }
    r = client.post("/api/segment-audit", json=unreachable)
    assert r.status_code == 400
    assert any(e["loc"] == "target" for e in r.json()["errors"])


def test_segment_audit_validation_errors_localized():
    bad = dict(AUDIT_CASE)
    bad["segments"] = [dict(s) for s in AUDIT_CASE["segments"]]
    bad["segments"][0]["delay"] = -1
    r = client.post("/api/segment-audit", json=bad)
    assert r.status_code == 400
    assert any(e["loc"] == "segments[0].delay" for e in r.json()["errors"])


def test_protected_paths_unchanged_by_audit_feature():
    """原保护规划接口的响应结构保持不变（不含审计字段）。"""
    r = client.post("/api/protected-paths", json={
        "source": "S", "target": "T",
        "segments": [
            {"id": "a", "from": "S", "to": "T", "delay": 1},
            {"id": "b", "from": "S", "to": "T", "delay": 2},
        ],
    })
    assert r.status_code == 200
    data = r.json()
    assert set(data) == {"status", "totalDelay", "paths"}
    assert data["totalDelay"] == 3
