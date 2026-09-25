"""最优段审计测试：必经/可替换/未选用归类 + 替代见证，全部用独立枚举对拍。

对拍口径（不信任求解器）：
  - 枚举全部简单 s-t 路径及其边不交无序对，得到最小总延迟与全部最优对；
  - 已用段为必经段  <=>  它出现在每一个最优对中；
  - 已用段为可替换段  <=>  存在一个最优对不含它；
  - 审计给出的替代见证本身必须是合法最优双路：两条链路各自连续、
    边互不重复、不含被审计段、总延迟与基准完全相同。
"""
from __future__ import annotations

import itertools
import random

from app.audit import MANDATORY, REPLACEABLE, UNUSED, audit_segments
from app.solver import Segment


def seg(sid, u, v, d):
    return Segment(sid, u, v, d)


def all_simple_paths(segments, source, target):
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
                dfs(nxt, used_edges | {ei}, used_nodes | {nxt},
                    cost + segments[ei].delay)

    dfs(source, frozenset(), {source}, 0)
    return paths


def optimal_pairs(segments, source, target):
    """返回 (最小总延迟, 全部最优边不交路径对)；无可行双路时返回 (None, [])。

    路径以**段标识集合**表示。
    """
    paths = all_simple_paths(segments, source, target)
    best, best_pairs = None, []
    for (idx1, c1), (idx2, c2) in itertools.combinations_with_replacement(paths, 2):
        if not idx1.isdisjoint(idx2):
            continue
        p1 = frozenset(segments[i].id for i in idx1)
        p2 = frozenset(segments[i].id for i in idx2)
        cand = c1 + c2
        if best is None or cand < best:
            best, best_pairs = cand, [(p1, p2)]
        elif cand == best:
            best_pairs.append((p1, p2))
    return best, best_pairs


def check_witness(segments, source, target, audit, baseline_total):
    """替代见证必须是不含被审计段、总延迟与基准相同的两条完整链路。"""
    witness = audit.witness
    assert witness is not None and len(witness) == 2
    used_ids = []
    for path in witness:
        chain = path.segments
        assert chain, "见证路径不能为空"
        assert chain[0].src == source and chain[-1].dst == target
        for a, b in zip(chain, chain[1:]):
            assert a.dst == b.src, "见证路径必须连续"
        assert path.delay == sum(s.delay for s in chain), "路径延迟必须可复算"
        used_ids.extend(s.id for s in chain)
    assert audit.segment.id not in used_ids, "见证必须不含被审计段"
    assert len(used_ids) == len(set(used_ids)), "见证两条路径必须边互不重复"
    assert sum(p.delay for p in witness) == baseline_total, "见证总延迟必须与基准相同"


def audit_against_oracle(segments, source, target):
    """用独立枚举复核一次审计结果，返回审计结果供进一步断言。"""
    result = audit_segments(segments, source, target)
    best, best_pairs = optimal_pairs(segments, source, target)
    if best is None:
        assert result.status in ("insufficient", "unreachable")
        return result
    assert result.status == "ok"
    assert result.total_delay == best

    baseline_ids = {s.id for p in result.baseline_paths for s in p.segments}
    assert len(baseline_ids) == sum(len(p.segments) for p in result.baseline_paths)
    assert sum(p.delay for p in result.baseline_paths) == best

    for a in result.audits:
        in_all = all(a.segment.id in (p1 | p2) for p1, p2 in best_pairs)
        in_some = any(a.segment.id in (p1 | p2) for p1, p2 in best_pairs)
        if a.segment.id not in baseline_ids:
            assert a.classification == UNUSED and a.witness is None
        elif in_all:
            assert a.classification == MANDATORY, (
                f"{a.segment.id} 出现在全部最优对中，必须判必经"
            )
            assert a.witness is None, "必经段不得伪造替代见证"
        else:
            assert in_some  # 已用段必然出现在某个最优对（基准本身）中
            assert a.classification == REPLACEABLE, (
                f"{a.segment.id} 存在不含它的最优对，必须判可替换"
            )
            check_witness(segments, source, target, a, result.total_delay)
    return result


# ---------- 定向用例 ----------


def test_unique_optimum_marks_used_segments_mandatory():
    """贪心反例图：全局最优唯一，已用段全部必经，未进方案的段为未选用。"""
    segments = [
        seg("e1", "S", "A", 1),
        seg("e2", "A", "B", 1),
        seg("e3", "B", "T", 1),
        seg("e4", "S", "B", 5),
        seg("e5", "A", "T", 5),
    ]
    r = audit_against_oracle(segments, "S", "T")
    assert r.total_delay == 12
    cls = {a.segment.id: a.classification for a in r.audits}
    assert cls == {
        "e1": MANDATORY, "e3": MANDATORY, "e4": MANDATORY, "e5": MANDATORY,
        "e2": UNUSED,
    }
    assert all(a.witness is None for a in r.audits)


def test_used_segment_replaceable_via_equal_cost_detour():
    """已用段 x 可被等延迟绕行替换：见证必须换掉 x 且总延迟不变。"""
    segments = [
        seg("e1", "S", "A", 0),
        seg("x", "A", "B", 2),
        seg("e2", "B", "T", 0),
        seg("e3", "S", "C", 0),
        seg("e4", "C", "T", 0),
        seg("e5", "A", "C", 1),
        seg("e6", "C", "B", 1),
    ]
    r = audit_against_oracle(segments, "S", "T")
    cls = {a.segment.id: a.classification for a in r.audits}
    assert cls["x"] == REPLACEABLE
    assert cls["e5"] == UNUSED and cls["e6"] == UNUSED
    assert all(cls[f"e{i}"] == MANDATORY for i in range(1, 5))
    wx = next(a for a in r.audits if a.segment.id == "x")
    wids = {s.id for p in wx.witness for s in p.segments}
    assert "x" not in wids
    assert {"e5", "e6"} <= wids  # 见证确实走了等延迟绕行
    assert sum(p.delay for p in wx.witness) == r.total_delay


def test_parallel_fibers_all_replaceable_when_spare_exists():
    """三条等延迟并行段：用两条，任一已用段都可被备用段替换。"""
    segments = [seg("p1", "S", "T", 3), seg("p2", "S", "T", 3), seg("p3", "S", "T", 3)]
    r = audit_against_oracle(segments, "S", "T")
    assert r.total_delay == 6
    cls = {a.segment.id: a.classification for a in r.audits}
    assert cls == {"p1": REPLACEABLE, "p2": REPLACEABLE, "p3": UNUSED}
    for a in r.audits:
        if a.classification == REPLACEABLE:
            wids = {s.id for p in a.witness for s in p.segments}
            assert wids == {"p1", "p2", "p3"} - {a.segment.id}


def test_parallel_fibers_without_spare_are_mandatory():
    """两条并行段都必须上场时，二者皆为必经段（含零延迟）。"""
    segments = [seg("p1", "S", "T", 0), seg("p2", "S", "T", 0)]
    r = audit_against_oracle(segments, "S", "T")
    assert r.total_delay == 0
    assert all(a.classification == MANDATORY for a in r.audits)
    assert all(a.witness is None for a in r.audits)


def test_zero_delay_segments_in_exchange_cycle():
    """零延迟段构成的零费用交换环：已用零延迟段同样可被替换。"""
    segments = [
        seg("a", "S", "A", 0),
        seg("b", "A", "B", 0),
        seg("c", "B", "T", 0),
        seg("d", "S", "C", 0),
        seg("e", "C", "T", 0),
        seg("f", "A", "C", 0),
        seg("g", "C", "B", 0),
    ]
    r = audit_against_oracle(segments, "S", "T")
    assert r.total_delay == 0
    cls = {a.segment.id: a.classification for a in r.audits}
    assert cls["b"] == REPLACEABLE  # 可经 f/g 零延迟绕行
    assert cls["a"] == MANDATORY and cls["d"] == MANDATORY  # 源点仅两条出边


def test_multiple_optima_can_still_have_mandatory_segments():
    """多个等价最优方案不等于可替换：共享段在全部最优方案中仍是必经段。

    零费用环 f/g 让 A、B 间走向可互换，产生两个总延迟同为 0 的最优
    双路（{a,b,c,d} 与 {a,f,g,d}），但 a 与 d 出现在每一个最优方案中。
    """
    segments = [
        seg("a", "S", "A", 0),
        seg("b", "A", "B", 0),
        seg("c", "B", "T", 0),
        seg("d", "S", "B", 0),
        seg("e", "A", "T", 0),
        seg("f", "A", "B", 0),
        seg("g", "B", "A", 0),
    ]
    best, best_pairs = optimal_pairs(segments, "S", "T")
    assert best == 0 and len(best_pairs) >= 2  # 确认确实存在多个等价最优
    r = audit_against_oracle(segments, "S", "T")
    cls = {a.segment.id: a.classification for a in r.audits}
    assert cls["a"] == MANDATORY and cls["d"] == MANDATORY
    assert all(a.witness is None for a in r.audits if a.classification == MANDATORY)


def test_equal_arm_diamond_has_unique_optimum_pair():
    """等臂菱形的唯一最优对是 {a,b}+{c,d}，四段全部必经。"""
    segments = [
        seg("a", "S", "A", 2),
        seg("b", "A", "T", 3),
        seg("c", "S", "B", 2),
        seg("d", "B", "T", 3),
    ]
    best, best_pairs = optimal_pairs(segments, "S", "T")
    assert best == 10 and len(best_pairs) == 1
    r = audit_against_oracle(segments, "S", "T")
    assert all(a.classification == MANDATORY for a in r.audits)


def test_audit_status_when_no_dual_path():
    segments = [seg("a", "S", "A", 1), seg("b", "A", "T", 1)]
    assert audit_segments(segments, "S", "T").status == "insufficient"
    segments = [seg("a", "S", "A", 1), seg("b", "B", "T", 1)]
    assert audit_segments(segments, "S", "T").status == "unreachable"


def test_audit_is_deterministic():
    """同一拓扑重复审计：归类与替代见证必须完全一致。"""
    segments = [
        seg("e1", "S", "A", 0),
        seg("x", "A", "B", 2),
        seg("e2", "B", "T", 0),
        seg("e3", "S", "C", 0),
        seg("e4", "C", "T", 0),
        seg("e5", "A", "C", 1),
        seg("e6", "C", "B", 1),
    ]

    def signature(r):
        return [
            (
                a.segment.id,
                a.classification,
                [[s.id for s in p.segments] for p in a.witness]
                if a.witness
                else None,
            )
            for a in r.audits
        ]

    assert signature(audit_segments(segments, "S", "T")) == signature(
        audit_segments(segments, "S", "T")
    )


# ---------- 随机图对拍 ----------


def test_exhaustive_random_graphs_match_enumeration_oracle():
    """随机有向多图（含零延迟与并行段）逐段对拍独立枚举口径。"""
    rng = random.Random(20260925)
    for _ in range(1500):
        n = rng.randint(3, 4)
        nodes = [f"v{i}" for i in range(n)]
        m = rng.randint(n - 1, n * (n - 1))
        candidates = [(u, v) for u, v in itertools.permutations(nodes, 2)]
        chosen = rng.sample(candidates, min(m, len(candidates)))
        segments, idx = [], 0
        for u, v in chosen:
            k = 1 if rng.random() < 0.75 else 2  # 偶尔并行
            for _ in range(k):
                segments.append(seg(f"s{idx}", u, v, rng.randint(0, 9)))
                idx += 1
        source, target = nodes[0], nodes[-1]
        result = audit_against_oracle(segments, source, target)
        if result.status == "ok":
            # 重复审计一致性
            again = audit_segments(segments, source, target)
            assert [a.classification for a in again.audits] == [
                a.classification for a in result.audits
            ]
