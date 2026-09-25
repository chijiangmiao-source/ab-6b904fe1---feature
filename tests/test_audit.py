"""最优段审计测试：必经/可替换判定、替代见证、确定性、HTTP 接口与蛮力对拍。"""
from __future__ import annotations

import itertools

from fastapi.testclient import TestClient

from app.audit import audit_optimal_segments
from app.main import app
from app.solver import Segment, solve_two_paths

client = TestClient(app)


def seg(sid, u, v, d):
    return Segment(sid, u, v, d)


def payload_of(segments, source="S", target="T", **extra):
    data = {
        "source": source,
        "target": target,
        "segments": [
            {"id": s.id, "from": s.src, "to": s.dst, "delay": s.delay}
            for s in segments
        ],
    }
    data.update(extra)
    return data


def check_witness(witness, excluded_id, source, target, baseline_total):
    """结构性校验：替代双路完整、边互不重复、不含被审段、总延迟等于基准。"""
    assert witness is not None
    assert len(witness.paths) == 2
    ids = []
    for p in witness.paths:
        assert p.segments, "替代链路不能为空"
        assert p.segments[0].src == source
        assert p.segments[-1].dst == target
        for a, b in zip(p.segments, p.segments[1:]):
            assert a.dst == b.src, "替代链路必须连续"
        assert sum(s.delay for s in p.segments) == p.delay
        ids.extend(s.id for s in p.segments)
    assert len(ids) == len(set(ids)), "替代双路必须边互不重复"
    assert excluded_id not in ids, "替代链路不得包含被审段"
    assert witness.total_delay == baseline_total
    assert sum(p.delay for p in witness.paths) == baseline_total


# ---------------------------------------------------------------- 单元测试


def test_unique_optimum_all_segments_mandatory():
    """贪心反例的最优双路唯一：全部已用段都是必经段，且不得给出见证。"""
    segments = [
        seg("e1", "S", "A", 1),
        seg("e2", "A", "B", 1),
        seg("e3", "B", "T", 1),
        seg("e4", "S", "B", 5),
        seg("e5", "A", "T", 5),
    ]
    r = audit_optimal_segments(segments, "S", "T")
    assert r.status == "ok"
    assert r.total_delay == 12
    assert {a.segment_id for a in r.audits} == {"e1", "e3", "e4", "e5"}
    for a in r.audits:
        assert a.classification == "mandatory"
        assert a.witness is None, "必经段不得伪造替代见证"


def test_multiple_optima_all_segments_replaceable():
    """三条等费路由：基准任取两条，每段都可由第三条路由等费替代。"""
    segments = [
        seg("a1", "S", "A", 1),
        seg("a2", "A", "T", 1),
        seg("b1", "S", "B", 1),
        seg("b2", "B", "T", 1),
        seg("c1", "S", "C", 1),
        seg("c2", "C", "T", 1),
    ]
    r = audit_optimal_segments(segments, "S", "T")
    assert r.status == "ok"
    assert r.total_delay == 4
    assert len(r.audits) == 4
    for a in r.audits:
        assert a.classification == "replaceable"
        check_witness(a.witness, a.segment_id, "S", "T", 4)


def test_zero_delay_and_parallel_segments_mixed():
    """零延迟段 + 并行段：p1(0) 必经，p2(3) 可由并行段 p3(3) 等费替换。"""
    segments = [
        seg("p1", "S", "T", 0),
        seg("p2", "S", "T", 3),
        seg("p3", "S", "T", 3),
    ]
    r = audit_optimal_segments(segments, "S", "T")
    assert r.status == "ok"
    assert r.total_delay == 3
    by_id = {a.segment_id: a for a in r.audits}
    assert by_id["p1"].classification == "mandatory"
    assert by_id["p1"].witness is None
    assert by_id["p2"].classification == "replaceable"
    check_witness(by_id["p2"].witness, "p2", "S", "T", 3)
    used = {s.id for p in by_id["p2"].witness.paths for s in p.segments}
    assert used == {"p1", "p3"}


def test_zero_delay_exchange_cycle_middle_segment():
    """零延迟交换环：中间段可经零延迟旁路等费替换。"""
    segments = [
        seg("a", "S", "X", 2),
        seg("b", "X", "T", 5),
        seg("c", "S", "Y", 2),
        seg("d", "Y", "T", 5),
        seg("z1", "X", "Y", 0),  # 零延迟旁路
        seg("z2", "Y", "X", 0),
        seg("e", "S", "Z", 2),
        seg("f", "Z", "T", 5),
    ]
    base = solve_two_paths(segments, "S", "T")
    assert base.status == "ok"
    r = audit_optimal_segments(segments, "S", "T")
    assert r.status == "ok"
    assert r.total_delay == base.total_delay
    for a in r.audits:
        if a.classification == "replaceable":
            check_witness(a.witness, a.segment_id, "S", "T", r.total_delay)
        else:
            assert a.witness is None


def test_per_segment_recheck_matches_full_audit():
    """按段复算：单段审计结果必须与全量审计完全一致。"""
    segments = [
        seg("p1", "S", "T", 0),
        seg("p2", "S", "T", 3),
        seg("p3", "S", "T", 3),
    ]
    full = audit_optimal_segments(segments, "S", "T")
    for a in full.audits:
        one = audit_optimal_segments(segments, "S", "T", segment_id=a.segment_id)
        assert one.status == "ok"
        assert len(one.audits) == 1
        single = one.audits[0]
        assert single.segment_id == a.segment_id
        assert single.classification == a.classification
        if a.witness is None:
            assert single.witness is None
        else:
            assert single.witness is not None
            assert single.witness.total_delay == a.witness.total_delay
            for pw, sw in zip(single.witness.paths, a.witness.paths):
                assert [s.id for s in pw.segments] == [s.id for s in sw.segments]


def test_audit_is_deterministic_across_repeats():
    """重复查看同一段：归类与替代见证必须保持一致。"""
    segments = [
        seg("a1", "S", "A", 1),
        seg("a2", "A", "T", 1),
        seg("b1", "S", "B", 1),
        seg("b2", "B", "T", 1),
        seg("c1", "S", "C", 1),
        seg("c2", "C", "T", 1),
    ]

    def snapshot():
        r = audit_optimal_segments(segments, "S", "T")
        return [
            (
                a.segment_id,
                a.classification,
                None
                if a.witness is None
                else tuple(
                    tuple(s.id for s in p.segments) for p in a.witness.paths
                ),
            )
            for a in r.audits
        ]

    first = snapshot()
    for _ in range(5):
        assert snapshot() == first


def test_unused_segment_classified_unused():
    """基准方案未使用的段：归类为 unused，不伪造见证。"""
    segments = [
        seg("e1", "S", "A", 1),
        seg("e2", "A", "B", 1),  # 共享光纤，最优方案不使用
        seg("e3", "B", "T", 1),
        seg("e4", "S", "B", 5),
        seg("e5", "A", "T", 5),
    ]
    r = audit_optimal_segments(segments, "S", "T", segment_id="e2")
    assert r.status == "ok"
    assert r.audits[0].segment_id == "e2"
    assert r.audits[0].classification == "unused"
    assert r.audits[0].witness is None


def test_audit_passthrough_insufficient_and_unreachable():
    """流不足/不可达时审计结果与规划求解一致。"""
    bottleneck = [
        seg("a", "S", "A", 0),
        seg("b", "A", "T", 0),
    ]
    r = audit_optimal_segments(bottleneck, "S", "T")
    assert r.status == "insufficient"
    assert r.cut is not None
    assert {s.id for s in r.cut.edges} == {"a"}

    r = audit_optimal_segments(
        [seg("a", "S", "A", 1), seg("b", "B", "T", 1)], "S", "T"
    )
    assert r.status == "unreachable"


def test_audit_baseline_matches_planning():
    """审计返回的基准方案必须与规划求解结果完全一致。"""
    segments = [
        seg("e1", "S", "A", 1),
        seg("e2", "A", "B", 1),
        seg("e3", "B", "T", 1),
        seg("e4", "S", "B", 5),
        seg("e5", "A", "T", 5),
    ]
    plan = solve_two_paths(segments, "S", "T")
    r = audit_optimal_segments(segments, "S", "T")
    assert r.total_delay == plan.total_delay
    for pa, pb in zip(r.paths, plan.paths):
        assert [s.id for s in pa.segments] == [s.id for s in pb.segments]
        assert pa.delay == pb.delay


def test_random_graphs_match_brute_force_classification():
    """随机图上：审计归类与独立蛮力（枚举全部最优方案）逐段对拍。"""
    import random

    rng = random.Random(20260925)
    checked = replaceable_seen = mandatory_seen = 0
    for _ in range(600):
        n = rng.randint(3, 5)
        nodes = [f"v{i}" for i in range(n)]
        m = rng.randint(n - 1, n * (n - 1))
        chosen = rng.sample(list(itertools.permutations(nodes, 2)), m)
        segments, idx = [], 0
        for u, v in chosen:
            k = 1 if rng.random() < 0.75 else 2  # 偶尔并行段
            for _ in range(k):
                # 小延迟取值制造大量等费最优与零延迟段
                segments.append(seg(f"s{idx}", u, v, rng.randint(0, 3)))
                idx += 1
        source, target = nodes[0], nodes[-1]

        r = audit_optimal_segments(segments, source, target)
        base = solve_two_paths(segments, source, target)
        assert r.status == base.status
        if r.status != "ok":
            continue
        assert r.total_delay == base.total_delay

        avoidable = _brute_force_avoidable(segments, source, target, r.total_delay)
        assert r.audits is not None and len(r.audits) >= 1
        audited_ids = set()
        for a in r.audits:
            audited_ids.add(a.segment_id)
            pos = next(i for i, s in enumerate(segments) if s.id == a.segment_id)
            expected = "replaceable" if pos in avoidable else "mandatory"
            assert a.classification == expected, (
                f"段 {a.segment_id} 归类 {a.classification} != 蛮力 {expected}"
            )
            if a.classification == "replaceable":
                replaceable_seen += 1
                check_witness(a.witness, a.segment_id, source, target, r.total_delay)
                # 见证所用段必须来自原始拓扑
                valid_ids = {s.id for s in segments}
                for p in a.witness.paths:
                    assert all(s.id in valid_ids for s in p.segments)
            else:
                mandatory_seen += 1
                assert a.witness is None, "必经段不得伪造替代见证"
        # 审计必须覆盖基准方案的全部已用段
        used = {s.id for p in base.paths for s in p.segments}
        assert audited_ids == used
        checked += 1
    assert checked > 0
    assert replaceable_seen > 0, "随机用例应覆盖可替换段"
    assert mandatory_seen > 0, "随机用例应覆盖必经段"


def _brute_force_avoidable(segments, source, target, baseline_total):
    """独立蛮力：返回"存在某个最优双路方案不含它"的段下标集合。"""
    adj = {}
    for i, s in enumerate(segments):
        adj.setdefault(s.src, []).append((s.dst, i))
    paths = []

    def dfs(node, used_edges, used_nodes, cost):
        if node == target:
            paths.append((frozenset(used_edges), cost))
            return
        for nxt, ei in adj.get(node, []):
            if ei not in used_edges and nxt not in used_nodes:
                dfs(
                    nxt,
                    used_edges | {ei},
                    used_nodes | {nxt},
                    cost + segments[ei].delay,
                )

    dfs(source, frozenset(), {source}, 0)
    avoidable = set()
    optimal_pairs = 0
    for (p1, c1), (p2, c2) in itertools.combinations_with_replacement(paths, 2):
        if p1.isdisjoint(p2) and c1 + c2 == baseline_total:
            optimal_pairs += 1
            union = p1 | p2
            for i in range(len(segments)):
                if i not in union:
                    avoidable.add(i)
    assert optimal_pairs >= 1, "基准总延迟必对应至少一个最优方案"
    return avoidable


# ---------------------------------------------------------------- HTTP 接口

GREEDY_PAYLOAD = {
    "source": "S",
    "target": "T",
    "segments": [
        {"id": "e1", "from": "S", "to": "A", "delay": 1},
        {"id": "e2", "from": "A", "to": "B", "delay": 1},
        {"id": "e3", "from": "B", "to": "T", "delay": 1},
        {"id": "e4", "from": "S", "to": "B", "delay": 5},
        {"id": "e5", "from": "A", "to": "T", "delay": 5},
    ],
}

MULTI_PAYLOAD = {
    "source": "S",
    "target": "T",
    "segments": [
        {"id": "a1", "from": "S", "to": "A", "delay": 1},
        {"id": "a2", "from": "A", "to": "T", "delay": 1},
        {"id": "b1", "from": "S", "to": "B", "delay": 1},
        {"id": "b2", "from": "B", "to": "T", "delay": 1},
        {"id": "c1", "from": "S", "to": "C", "delay": 1},
        {"id": "c2", "from": "C", "to": "T", "delay": 1},
    ],
}


def _check_witness_json(witness, excluded_id, source, target, baseline_total):
    assert witness is not None
    assert witness["totalDelay"] == baseline_total
    paths = witness["paths"]
    assert len(paths) == 2
    ids = []
    for p in paths:
        segs = p["segments"]
        assert segs[0]["from"] == source and segs[-1]["to"] == target
        for a, b in zip(segs, segs[1:]):
            assert a["to"] == b["from"]
        assert sum(s["delay"] for s in segs) == p["delay"]
        ids.extend(s["id"] for s in segs)
    assert len(ids) == len(set(ids))
    assert excluded_id not in ids
    assert sum(p["delay"] for p in paths) == baseline_total


def test_api_audit_unique_optimum_all_mandatory():
    r = client.post("/api/segment-audit", json=GREEDY_PAYLOAD)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["totalDelay"] == 12
    assert {a["segmentId"] for a in data["audits"]} == {"e1", "e3", "e4", "e5"}
    for a in data["audits"]:
        assert a["classification"] == "mandatory"
        assert a["witness"] is None


def test_api_audit_baseline_equals_planning_response():
    plan = client.post("/api/protected-paths", json=GREEDY_PAYLOAD).json()
    audit = client.post("/api/segment-audit", json=GREEDY_PAYLOAD).json()
    assert audit["paths"] == plan["paths"]
    assert audit["totalDelay"] == plan["totalDelay"]


def test_api_audit_replaceable_witness():
    r = client.post("/api/segment-audit", json=MULTI_PAYLOAD)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["totalDelay"] == 4
    assert len(data["audits"]) == 4
    for a in data["audits"]:
        assert a["classification"] == "replaceable"
        _check_witness_json(a["witness"], a["segmentId"], "S", "T", 4)


def test_api_audit_per_segment_recheck():
    full = client.post("/api/segment-audit", json=MULTI_PAYLOAD).json()
    for a in full["audits"]:
        r = client.post(
            "/api/segment-audit",
            json={**MULTI_PAYLOAD, "segmentId": a["segmentId"]},
        )
        assert r.status_code == 200
        one = r.json()
        assert one["status"] == "ok"
        assert len(one["audits"]) == 1
        assert one["audits"][0] == a, "按段复算必须与全量审计一致"


def test_api_audit_deterministic_over_http():
    r1 = client.post("/api/segment-audit", json=MULTI_PAYLOAD).json()
    r2 = client.post("/api/segment-audit", json=MULTI_PAYLOAD).json()
    assert r1 == r2


def test_api_audit_segment_id_errors():
    r = client.post("/api/segment-audit", json={**GREEDY_PAYLOAD, "segmentId": "zz"})
    assert r.status_code == 400
    assert any(e["loc"] == "segmentId" for e in r.json()["errors"])

    r = client.post("/api/segment-audit", json={**GREEDY_PAYLOAD, "segmentId": 5})
    assert r.status_code == 400
    assert any(e["loc"] == "segmentId" for e in r.json()["errors"])


def test_api_audit_unused_segment():
    r = client.post(
        "/api/segment-audit", json={**GREEDY_PAYLOAD, "segmentId": "e2"}
    )
    assert r.status_code == 200
    audits = r.json()["audits"]
    assert len(audits) == 1
    assert audits[0]["classification"] == "unused"
    assert audits[0]["witness"] is None


def test_api_audit_validation_and_unreachable():
    bad = {
        "source": "S",
        "target": "T",
        "segments": [{"id": "e1", "from": "S", "to": "T", "delay": -2}],
    }
    r = client.post("/api/segment-audit", json=bad)
    assert r.status_code == 400
    assert any(e["loc"] == "segments[0].delay" for e in r.json()["errors"])

    unreachable = {
        "source": "S",
        "target": "T",
        "segments": [
            {"id": "e1", "from": "S", "to": "A", "delay": 1},
            {"id": "e2", "from": "B", "to": "T", "delay": 1},
        ],
    }
    r = client.post("/api/segment-audit", json=unreachable)
    assert r.status_code == 400
    assert any(e["loc"] == "target" for e in r.json()["errors"])


def test_api_audit_insufficient_mirrors_cut():
    r = client.post(
        "/api/segment-audit",
        json={
            "source": "S",
            "target": "T",
            "segments": [
                {"id": "a", "from": "S", "to": "A", "delay": 0},
                {"id": "b", "from": "A", "to": "T", "delay": 0},
            ],
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "insufficient"
    assert {e["id"] for e in data["cut"]["edges"]} == {"a"}
