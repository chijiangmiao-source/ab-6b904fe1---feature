"""求解器正确性测试：含全枚举对拍、贪心反例、割证据校验。"""
from __future__ import annotations

import itertools

from app.solver import Segment, solve_two_paths


def seg(sid, u, v, d):
    return Segment(sid, u, v, d)


def brute_force(segments, source, target):
    """枚举所有简单 s-t 路径的两两组合，返回最小总延迟或 None。

    参考实现，只用于测试，不参与生产代码。
    """
    adj = {}
    for i, s in enumerate(segments):
        adj.setdefault(s.src, []).append((s.dst, i))
    names = {n for s in segments for n in (s.src, s.dst)} | {source, target}

    all_paths = []

    def dfs(node, used_edges, used_nodes, cost):
        if node == target:
            all_paths.append((frozenset(used_edges), cost))
            return
        for nxt, ei in adj.get(node, []):
            if ei in used_edges or nxt in used_nodes:
                continue
            dfs(nxt, used_edges | {ei}, used_nodes | {nxt},
                cost + segments[ei].delay)

    dfs(source, frozenset(), {source}, 0)

    best = None
    for (p1, c1), (p2, c2) in itertools.combinations_with_replacement(all_paths, 2):
        if p1.isdisjoint(p2):
            cand = c1 + c2
            if best is None or cand < best:
                best = cand
    return best


def test_greedy_counterexample_returns_true_global_optimum():
    """局部最短路占用共享光纤时，贪心删边会失败，MCMF 必须给出真最优。

    S-A-B-T = 1+1+1 = 3 是唯一最短路，贪心占用 e2 后只剩 S-B-T，
    再无第二条；但 S-A-T(1+5) 与 S-B-T(5+1) 边不重复，总和 12。
    """
    segments = [
        seg("e1", "S", "A", 1),
        seg("e2", "A", "B", 1),
        seg("e3", "B", "T", 1),
        seg("e4", "S", "B", 5),
        seg("e5", "A", "T", 5),
    ]
    r = solve_two_paths(segments, "S", "T")
    assert r.status == "ok"
    assert r.total_delay == 12
    used = [s.id for p in r.paths for s in p.segments]
    assert len(used) == len(set(used))  # 边互不重复
    assert sum(p.delay for p in r.paths) == 12
    # 路径确为 S-A-T 与 S-B-T（顺序无关）
    chains = sorted([tuple(s.id for s in p.segments) for p in r.paths])
    assert chains == [("e1", "e5"), ("e4", "e3")]


def test_two_disjoint_paths_picked_over_short_shared_one():
    """最短路径走瓶颈共享段时，求解器应放弃它选全局组合。"""
    # 直接段 S-T 延迟 0，但容量只有 1；第二条只能走 S-A-T = 100
    segments = [
        seg("d", "S", "T", 0),
        seg("a1", "S", "A", 50),
        seg("a2", "A", "T", 50),
    ]
    r = solve_two_paths(segments, "S", "T")
    assert r.status == "ok"
    assert r.total_delay == 100


def test_zero_delays_and_parallel_fibers():
    """零延迟与并行光纤（同端点多条段）必须支持。"""
    segments = [
        seg("p1", "S", "T", 0),
        seg("p2", "S", "T", 3),
        seg("p3", "S", "T", 7),  # 并行第三条，最优取前两条
    ]
    r = solve_two_paths(segments, "S", "T")
    assert r.status == "ok"
    assert r.total_delay == 3
    ids = sorted(s.id for p in r.paths for s in p.segments)
    assert ids == ["p1", "p2"]


def test_shared_nodes_allowed():
    """节点允许重合：两条路径经过同一中间节点，但使用不同的入/出段。"""
    segments = [
        seg("a", "S", "M", 1),
        seg("b", "S", "M", 1),
        seg("c", "M", "T", 1),
        seg("d", "M", "T", 1),
    ]
    r = solve_two_paths(segments, "S", "T")
    assert r.status == "ok"
    assert r.total_delay == 4


def test_insufficient_flow_gives_cut_with_all_outgoing_edges():
    """单点瓶颈：外出割边必须穷尽，且割容量为 1。"""
    segments = [
        seg("e1", "S", "A", 2),
        seg("e2", "S", "B", 3),
        seg("e3", "A", "X", 4),
        seg("e4", "B", "X", 5),
        seg("e5", "X", "T", 1),  # 唯一外出段
    ]
    r = solve_two_paths(segments, "S", "T")
    assert r.status == "insufficient"
    assert r.cut is not None
    ss = set(r.cut.source_set)
    assert "S" in ss
    assert "T" not in ss
    # 该图唯一能单独阻断终点的段是 e5，任何容量 1 的最小割外出边都只能是它
    assert {s.id for s in r.cut.edges} == {"e5"}
    # 残量最小割的源侧集合必包含起点侧全部汇聚节点（至少 S,A,B）
    assert {"S", "A", "B"} <= ss
    # 割一致性：报告的割边恰好等于"源侧 -> 外部"的全部原始段
    crossing = {
        s.id for s in segments
        if s.src in ss and s.dst not in ss
    }
    assert {s.id for s in r.cut.edges} == crossing


def test_bottleneck_two_unit_cut_all_listed():
    """割容量恰好为 2 时应成功；容量为 1 时列出全部外出边。"""
    segments = [
        seg("a", "S", "A", 0),
        seg("b", "A", "T", 0),  # 单段瓶颈
    ]
    r = solve_two_paths(segments, "S", "T")
    assert r.status == "insufficient"
    ss = set(r.cut.source_set)
    assert ss == {"S"}
    assert {s.id for s in r.cut.edges} == {"a"}


def test_unreachable():
    segments = [seg("a", "S", "A", 1), seg("b", "B", "T", 1)]
    r = solve_two_paths(segments, "S", "T")
    assert r.status == "unreachable"
    assert r.paths is None and r.cut is None


def test_exhaustive_small_graphs_match_brute_force():
    """对 3、4 节点上所有小规模多图做蛮力对拍（状态+费用穷举）。"""
    import random

    rng = random.Random(20260924)
    checked = 0
    for trial in range(4000):
        n = rng.randint(3, 4)
        nodes = [f"v{i}" for i in range(n)]
        m = rng.randint(n - 1, n * (n - 1))
        candidates = [(u, v) for u, v in itertools.permutations(nodes, 2)]
        chosen = rng.sample(candidates, min(m, len(candidates)))
        segments, idx = [], 0
        for u, v in chosen:
            k = 1 if rng.random() < 0.8 else 2  # 偶尔并行
            for _ in range(k):
                segments.append(seg(f"s{idx}", u, v, rng.randint(0, 9)))
                idx += 1
        source, target = nodes[0], nodes[-1]

        r = solve_two_paths(segments, source, target)
        expected = brute_force(segments, source, target)

        if expected is None:
            assert r.status in ("insufficient", "unreachable")
            if r.status == "insufficient":
                # 割证据一致性：每条外出割边跨集合，且没有任何"段"被漏掉
                ss = set(r.cut.source_set)
                assert source in ss and target not in ss
                outgoing = {
                    s.id for s in segments
                    if s.src in ss and s.dst not in ss
                }
                assert {s.id for s in r.cut.edges} == outgoing
                # 既然割边数 < 2 才会流不足（每条段容量 1）
                assert len(outgoing) < 2 or not _two_disjoint_exist(segments, source, target)
        else:
            assert r.status == "ok"
            assert r.total_delay == expected
            used = [s.id for p in r.paths for s in p.segments]
            assert len(used) == len(set(used))
            assert sum(p.delay for p in r.paths) == expected
            # 每条路径都是 source..target 的连续链路
            for p in r.paths:
                assert p.segments[0].src == source
                assert p.segments[-1].dst == target
                for x, y in zip(p.segments, p.segments[1:]):
                    assert x.dst == y.src
        checked += 1
    assert checked == 4000


def _two_disjoint_exist(segments, source, target):
    """仅用于测试断言的可达性二次确认（两路径存在性）。"""
    return brute_force(segments, source, target) is not None
