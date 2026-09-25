"""最优段审计：判定基准双路中各已用段是否在全部最小总延迟方案中不可替代。

判定基于**最优二单位流残量网络中的零费用交换环**，而非逐段重新求解：

  设 f 为最优二单位流（总延迟 C），e=(u,v) 为载流段、延迟 d。任意最优流
  f' 与 f 的差是残余网络中一个费用为 0 的环流；f' 不含 e 当且仅当该环流
  经过 e 的反向边 (v,u)。因此

      e 可替换  ⟺  残余网络存在经过 (v,u) 的零费用交换环
               ⟺  残余网络中 u→v 的最短路费用恰为 d
               （若小于 d，则 (v,u) 与该路构成负费用环，与 f 最优矛盾，
                 故只可能大于或等于 d）。

  判定为可替换时，沿找到的零费用环推一单位流，即得一组不含 e、总延迟与
  基准完全相同的两条完整替代链路（见证）；判定为必经段时不提供任何见证，
  绝不伪造。

零延迟段、并行段与多个等价最优方案均由同一准则自然覆盖；所有最短路按
(折算费用, 节点编号) 确定性选取，故重复审计同一段的归类与见证保持一致。
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass

from .solver import (
    REQUIRED_FLOW,
    CutResult,
    FlowState,
    PathResult,
    Segment,
    compute_optimal_flow,
    decompose_paths,
    min_cut,
)

INF = float("inf")


@dataclass
class Witness:
    """可替换段的替代双路：不含被审段、总延迟与基准完全相同。"""

    paths: list[PathResult]
    total_delay: int


@dataclass
class SegmentAudit:
    segment_id: str
    # "mandatory"（必经段）| "replaceable"（可替换）| "unused"（基准方案未使用）
    classification: str
    witness: Witness | None = None


@dataclass
class AuditResult:
    status: str  # "ok" | "insufficient" | "unreachable"
    paths: list[PathResult] | None = None  # 基准双路（与规划接口一致）
    total_delay: int | None = None
    audits: list[SegmentAudit] | None = None
    cut: CutResult | None = None


def _johnson_potentials(state: FlowState) -> list[int]:
    """对残余网络做 Bellman-Ford（虚拟源点），得到使折算费用非负的势函数。"""
    n = len(state.names)
    h = [0] * n
    for _ in range(n - 1):
        updated = False
        for u in range(n):
            for e in state.graph[u]:
                if e[2] > 0 and h[u] + e[3] < h[e[0]]:
                    h[e[0]] = h[u] + e[3]
                    updated = True
        if not updated:
            break
    # 最优流的残余网络必无负费用环；防御性校验，发现即视为内部错误。
    for u in range(n):
        for e in state.graph[u]:
            if e[2] > 0 and h[u] + e[3] < h[e[0]]:
                raise RuntimeError("最优流残余网络出现负费用环，内部状态异常")
    return h


def _shortest_residual_path(
    state: FlowState, h: list[int], start: int, goal: int
) -> tuple[int | None, list[list] | None]:
    """残余网络中 start→goal 的最短路（势函数折算 + Dijkstra，确定性）。

    返回 (实际费用, 途经残余边列表)；不可达时返回 (None, None)。
    """
    n = len(state.names)
    dist = [INF] * n
    prev_node = [-1] * n
    prev_edge = [-1] * n
    dist[start] = 0
    heap = [(0, start)]
    while heap:
        d, v = heapq.heappop(heap)
        if d > dist[v]:
            continue
        for i, e in enumerate(state.graph[v]):
            if e[2] <= 0:
                continue
            nd = d + e[3] + h[v] - h[e[0]]
            if nd < dist[e[0]]:
                dist[e[0]] = nd
                prev_node[e[0]] = v
                prev_edge[e[0]] = i
                heapq.heappush(heap, (nd, e[0]))
    if dist[goal] == INF:
        return None, None
    actual = dist[goal] - h[start] + h[goal]
    edges: list[list] = []
    v = goal
    while v != start:
        edges.append(state.graph[prev_node[v]][prev_edge[v]])
        v = prev_node[v]
    edges.reverse()
    return actual, edges


def _decompose_witness_flow(
    flow: list[int], segments: list[Segment], state: FlowState
) -> list[PathResult]:
    """把 0/1 二单位流分解为两条简单链路（途中摘除零费用环流）。

    最优流中可能残留零费用环流（如零延迟反向并行段）；其费用必为 0，
    摘除后两条 s-t 链路的总延迟仍等于基准总延迟。
    """
    adjacency: dict[int, list[int]] = {}
    for pos, seg in enumerate(segments):
        if flow[pos]:
            adjacency.setdefault(state.index_of[seg.src], []).append(pos)

    paths: list[PathResult] = []
    for _ in range(REQUIRED_FLOW):
        chosen: list[int] = []
        node = state.source
        depart = {state.source: 0}  # 节点 -> 在 chosen 中从该节点出发的下标
        while node != state.target:
            pos = adjacency[node].pop()
            chosen.append(pos)
            node = state.index_of[segments[pos].dst]
            if node != state.target and node in depart:
                # 回到已访问节点：chosen[depart[node]:] 为零费用环，摘除
                del chosen[depart[node]:]
                depart = {}
                for i, p in enumerate(chosen):
                    depart[state.index_of[segments[p].src]] = i
                depart[node] = len(chosen)
            else:
                depart[node] = len(chosen)
        segs = [segments[p] for p in chosen]
        paths.append(PathResult(segs, sum(s.delay for s in segs)))
    return paths


def _verify_witness(
    witness: Witness, excluded_id: str, state: FlowState, baseline_total: int
) -> None:
    """见证自检：任何一项不满足都说明内部实现有误——宁可报错也不得伪造。"""
    source = state.names[state.source]
    target = state.names[state.target]
    problems: list[str] = []
    if len(witness.paths) != REQUIRED_FLOW:
        problems.append("替代链路数量不为 2")
    used_ids: list[str] = []
    for p in witness.paths:
        if not p.segments:
            problems.append("替代链路为空")
            continue
        if p.segments[0].src != source or p.segments[-1].dst != target:
            problems.append(f"链路端点不是 {source}→{target}")
        for a, b in zip(p.segments, p.segments[1:]):
            if a.dst != b.src:
                problems.append("替代链路不连续")
        if sum(s.delay for s in p.segments) != p.delay:
            problems.append("替代链路延迟不可复算")
        used_ids.extend(s.id for s in p.segments)
    if len(used_ids) != len(set(used_ids)):
        problems.append("替代双路的边并非互不重复")
    if excluded_id in used_ids:
        problems.append(f"替代链路仍含被审段 {excluded_id}")
    if witness.total_delay != baseline_total:
        problems.append(
            f"替代方案总延迟 {witness.total_delay} 与基准 {baseline_total} 不一致"
        )
    if problems:
        raise RuntimeError("替代见证自检失败：" + "；".join(problems))


def _build_witness(
    state: FlowState,
    segments: list[Segment],
    audited_pos: int,
    cycle_path_edges: list[list],
    baseline_total: int,
) -> Witness:
    """沿零费用交换环推一单位流，分解出不含被审段的两条替代链路。"""
    flow = [1 if fwd[2] == 0 else 0 for fwd in state.forward_edges]
    for e in cycle_path_edges:
        pos = e[4]
        if state.forward_edges[pos] is e:
            flow[pos] = 1  # 前向残余边：空载段转为载流
        else:
            flow[pos] = 0  # 反向残余边：抵消该段原有流量
    flow[audited_pos] = 0  # 环经被审段的反向边：其流量被抵消
    paths = _decompose_witness_flow(flow, segments, state)
    witness = Witness(paths=paths, total_delay=sum(p.delay for p in paths))
    _verify_witness(witness, segments[audited_pos].id, state, baseline_total)
    return witness


def _audit_used_segment(
    state: FlowState,
    h: list[int],
    segments: list[Segment],
    pos: int,
    baseline_total: int,
) -> SegmentAudit:
    """对基准方案中的一条已用段做零费用交换环判定。"""
    seg = segments[pos]
    u = state.index_of[seg.src]
    v = state.index_of[seg.dst]
    path_cost, path_edges = _shortest_residual_path(state, h, u, v)
    if path_cost is None or path_cost > seg.delay:
        # 无 u→v 残余路径或任何交换都更贵：该段出现在每一个最小总延迟
        # 双路方案中，属必经段，不提供替代见证。
        return SegmentAudit(segment_id=seg.id, classification="mandatory")
    if path_cost < seg.delay:
        # 与最优性矛盾（负费用环），防御性报错。
        raise RuntimeError("残余网络出现负费用交换环，内部状态异常")
    # path_cost == seg.delay：零费用交换环存在，沿环推流得到等费替代双路。
    witness = _build_witness(state, segments, pos, path_edges, baseline_total)
    return SegmentAudit(
        segment_id=seg.id, classification="replaceable", witness=witness
    )


def audit_optimal_segments(
    segments: list[Segment],
    source: str,
    target: str,
    segment_id: str | None = None,
) -> AuditResult:
    """对基准双路中的已用段逐段判定必经/可替换。

    segment_id 为 None 时审计全部已用段（按基准链路顺序）；否则只审计
    指定段（按段复算）。所有判定只基于同一次最优二单位流的残余网络，
    不逐段重新求解；同一输入的重复审计结果完全一致。
    """
    state = compute_optimal_flow(segments, source, target)
    if state.flow == 0:
        return AuditResult(status="unreachable")
    if state.flow < REQUIRED_FLOW:
        return AuditResult(status="insufficient", cut=min_cut(state, segments))

    baseline_paths = decompose_paths(state, segments)
    baseline_total = sum(p.delay for p in baseline_paths)
    pos_of_id = {seg.id: pos for pos, seg in enumerate(segments)}
    used_positions = [
        pos_of_id[seg.id] for path in baseline_paths for seg in path.segments
    ]

    if segment_id is not None:
        if segment_id not in pos_of_id:
            raise ValueError(f"未知段标识：{segment_id}")
        if segment_id not in {
            segments[pos].id for pos in used_positions
        }:
            # 段未参与基准双路：基准本身即不含该段的最优方案，无需另行见证。
            return AuditResult(
                status="ok",
                paths=baseline_paths,
                total_delay=baseline_total,
                audits=[SegmentAudit(segment_id, "unused")],
            )
        positions = [pos_of_id[segment_id]]
    else:
        positions = used_positions

    h = _johnson_potentials(state)
    audits = [
        _audit_used_segment(state, h, segments, pos, baseline_total)
        for pos in positions
    ]
    return AuditResult(
        status="ok",
        paths=baseline_paths,
        total_delay=baseline_total,
        audits=audits,
    )
