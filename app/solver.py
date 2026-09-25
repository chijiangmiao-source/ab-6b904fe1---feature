"""束线保护链路求解器。

在起点与终点之间求两条**边互不重复**（edge-disjoint）的有向路径，
使两条路径的总延迟之和最小。节点允许重合，允许并行光纤（同一对
端点之间可有多条段，只要段标识不同）。

算法：把每段光纤视为容量 1、费用为延迟的弧，求流量为 2 的最小费用流
（连续最短路 SSP + Johnson 势函数 + Dijkstra）。这保证得到全局最优，
而不是"先求一条最短路再删边"的贪心近似——后者在局部最短路占用共享
光纤时，会漏掉另行存在的可行双路组合。

若最大流不足 2（无法形成双路），则在残余网络上从起点可达的节点集合
即最小割的源侧集合，所有由该集合指向外部的原始弧即全部外出割边，
用于向工程师解释瓶颈如何阻断保护链路。

`compute_optimal_flow` 返回的残余网络快照同时供"最优段审计"复用：
审计判定只读取该快照，不改动求解结果。
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass

INF = float("inf")
REQUIRED_FLOW = 2


@dataclass(frozen=True)
class Segment:
    id: str
    src: str
    dst: str
    delay: int


@dataclass
class PathResult:
    segments: list[Segment]
    delay: int


@dataclass
class CutResult:
    source_set: list[str]
    edges: list[Segment]


@dataclass
class SolveResult:
    status: str  # "ok" | "insufficient" | "unreachable"
    paths: list[PathResult] | None = None
    total_delay: int | None = None
    cut: CutResult | None = None


@dataclass
class FlowState:
    """最小费用流计算后的残余网络快照。

    graph 为残余网络邻接表，边为列表 [to, rev_index, cap, cost, seg_pos]：
    前向边与反向边的 seg_pos 都是对应原始段的下标（反向边即抵消该段流量）；
    forward_edges[pos] 即第 pos 条原始段的前向边对象本身
    （残余容量 0 表示该段载流 1，可用 `is` 与残余边比对方向）。
    """

    names: list[str]
    index_of: dict[str, int]
    source: int
    target: int
    graph: list[list[list]]
    forward_edges: list[list]
    flow: int


def compute_optimal_flow(
    segments: list[Segment], source: str, target: str
) -> FlowState:
    """容量 1、费用为延迟的最小费用流（SSP + Johnson 势函数 + Dijkstra）。"""
    index_of: dict[str, int] = {}
    names: list[str] = []

    def node_id(name: str) -> int:
        if name not in index_of:
            index_of[name] = len(names)
            names.append(name)
        return index_of[name]

    s = node_id(source)
    t = node_id(target)
    for seg in segments:
        node_id(seg.src)
        node_id(seg.dst)

    n = len(names)
    # 边: [to, rev_index, cap, cost, segment_pos]，用列表以便原地修改残余容量。
    graph: list[list[list]] = [[] for _ in range(n)]
    forward_edges: list[list] = []

    for pos, seg in enumerate(segments):
        u, v = index_of[seg.src], index_of[seg.dst]
        fwd = [v, len(graph[v]), 1, seg.delay, pos]
        rev = [u, len(graph[u]), 0, -seg.delay, pos]
        graph[u].append(fwd)
        graph[v].append(rev)
        forward_edges.append(fwd)

    # ---- 最小费用流：连续最短路（势函数保证 Dijkstra 的边权非负） ----
    flow = 0
    potential = [0] * n
    prev_node = [-1] * n
    prev_edge = [-1] * n

    while flow < REQUIRED_FLOW:
        dist = [INF] * n
        dist[s] = 0
        heap = [(0, s)]
        while heap:
            d, v = heapq.heappop(heap)
            if d > dist[v]:
                continue
            for i, e in enumerate(graph[v]):
                if e[2] <= 0:
                    continue
                nd = d + e[3] + potential[v] - potential[e[0]]
                if nd < dist[e[0]]:
                    dist[e[0]] = nd
                    prev_node[e[0]] = v
                    prev_edge[e[0]] = i
                    heapq.heappush(heap, (nd, e[0]))
        if dist[t] == INF:
            break
        for v in range(n):
            if dist[v] < INF:
                potential[v] += dist[v]
        v = t
        while v != s:
            e = graph[prev_node[v]][prev_edge[v]]
            e[2] -= 1
            graph[v][e[1]][2] += 1
            v = prev_node[v]
        flow += 1

    return FlowState(names, index_of, s, t, graph, forward_edges, flow)


def min_cut(state: FlowState, segments: list[Segment]) -> CutResult:
    """残余网络中从起点可达的集合（最小割源侧）与全部外出割边。"""
    n = len(state.names)
    reachable = [False] * n
    reachable[state.source] = True
    stack = [state.source]
    while stack:
        v = stack.pop()
        for e in state.graph[v]:
            if e[2] > 0 and not reachable[e[0]]:
                reachable[e[0]] = True
                stack.append(e[0])
    cut_edges = [
        seg
        for seg in segments
        if reachable[state.index_of[seg.src]]
        and not reachable[state.index_of[seg.dst]]
    ]
    source_set = [state.names[i] for i in range(n) if reachable[i]]
    return CutResult(source_set, cut_edges)


def decompose_paths(state: FlowState, segments: list[Segment]) -> list[PathResult]:
    """沿流量为 1 的原始弧从起点走到终点，走 REQUIRED_FLOW 遍。"""
    adjacency: dict[int, list[int]] = {}
    for pos, seg in enumerate(segments):
        if state.forward_edges[pos][2] == 0:  # 容量耗尽 => 承载 1 单位流量
            adjacency.setdefault(state.index_of[seg.src], []).append(pos)

    paths: list[PathResult] = []
    for _ in range(REQUIRED_FLOW):
        v = state.source
        chosen: list[Segment] = []
        while v != state.target:
            pos = adjacency[v].pop()
            chosen.append(segments[pos])
            v = state.index_of[segments[pos].dst]
        paths.append(PathResult(chosen, sum(seg.delay for seg in chosen)))
    return paths


def solve_two_paths(
    segments: list[Segment], source: str, target: str
) -> SolveResult:
    """求 source -> target 的两条边互不重复、总延迟最小的路径。

    返回:
      - "ok":           paths 为两条路径, total_delay 为可复算的最小总延迟
      - "insufficient": 存在路径但不足两条, cut 给出源侧节点集合与全部外出割边
      - "unreachable":  起点到终点根本不可达
    """
    state = compute_optimal_flow(segments, source, target)

    if state.flow == 0:
        return SolveResult(status="unreachable")

    if state.flow < REQUIRED_FLOW:
        return SolveResult(status="insufficient", cut=min_cut(state, segments))

    paths = decompose_paths(state, segments)
    return SolveResult(
        status="ok",
        paths=paths,
        total_delay=sum(p.delay for p in paths),
    )
