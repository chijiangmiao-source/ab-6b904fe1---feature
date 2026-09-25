"""最优段审计：判定基准双路方案中的每段光纤是否不可替代。

理论依据（最小费用流最优性条件）：
  设 f* 为流量 2 的最小费用流（即规划给出的基准双路）。若残余网络中
  存在可行势 pi（对每条残余弧 (x->y) 都有 rc = cost + pi[x] - pi[y] >= 0），
  则任意最优流与 f* 的差恰好是**零费用残余环**的叠加。因此：

  - 已用段 e = (u,v) 可替换  <=>  e 的反向弧 v->u 落在某个零费用残余
    环上。由于环上各弧折算费用之和恒等于环的真实费用（势函数沿环
     telescoping 抵消），且可行势下所有残余弧折算费用非负，这等价于：
    **反向弧本身折算费用为 0，且 u、v 在零费用残余子图 G0 中处于同一
    强连通分量**。此时沿 u->v 的零费用路径接上该反向弧即构成零费用
    交换环，沿环推一单位流量得到一个**不含 e、总延迟与基准完全相同**
    的最优双路——审计直接把这个交换结果分解为两条完整链路作为替代
    见证，而不是逐段重新求解规划问题。
  - 上述条件不成立  <=>  任何最优双路都必须使用 e，e 为**必经段**，
    此时绝不伪造替代见证（witness 为空）。
  - 基准未使用的段归类为"未选用"，不判定必经性。

关键细节：不能默认"已用段的反向弧折算费用必为 0"。SSP 增广过程中
维护的势对后续不可达节点会失效，而审计在**最终残余网络**上用超级源
Bellman-Ford（Johnson）重算的可行势只是众多可行对偶解之一——未落在
零费用环上的已用段，其反向弧折算费用可能严格为正（该段恰为必经段）。
最优流的残余网络无负环，可行势一定存在。

零延迟段、并行段、多个等价最优方案均被同一判据自然覆盖：
零费用环只关心残余弧的折算费用是否为 0，与段延迟是否为 0、是否
并行、等价方案有多少个无关。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .solver import (
    REQUIRED_FLOW,
    PathResult,
    Segment,
    _build_network,
    _decompose_paths,
    _min_cost_flow,
)

# 归类取值
MANDATORY = "mandatory"      # 必经段：全部最小总延迟双路方案都使用它
REPLACEABLE = "replaceable"  # 可替换段：存在不含它且总延迟相同的最优双路
UNUSED = "unused"            # 未选用：基准方案未使用该段


@dataclass
class SegmentAudit:
    segment: Segment
    used_in_baseline: bool
    classification: str            # mandatory | replaceable | unused
    witness: list[PathResult] | None = None  # 仅可替换段携带，两条完整替代链路


@dataclass
class AuditResult:
    status: str  # "ok" | "insufficient" | "unreachable"
    source: str = ""
    target: str = ""
    baseline_paths: list[PathResult] = field(default_factory=list)
    total_delay: int | None = None
    audits: list[SegmentAudit] = field(default_factory=list)


def _feasible_potentials(graph: list[list[list]]) -> list[int]:
    """在最终残余网络上用超级源 Bellman-Ford 求可行势。

    最优流的残余网络不存在负费用环，故势一定存在；从虚拟源向所有
    节点连 0 费用弧，等价于把 dist 初始化为全 0 后做 n 轮松弛。
    """
    n = len(graph)
    dist = [0] * n
    for _ in range(n):
        updated = False
        for v in range(n):
            dv = dist[v]
            for e in graph[v]:
                if e[2] > 0 and dv + e[3] < dist[e[0]]:
                    dist[e[0]] = dv + e[3]
                    updated = True
        if not updated:
            break
    else:  # n 轮后仍可松弛 => 存在负环，与最优性矛盾
        raise AssertionError("最优流残余网络出现负费用环")
    return dist


def _zero_cost_adjacency(
    graph: list[list[list]], potential: list[int]
) -> list[list[tuple[int, int]]]:
    """零费用残余子图 G0：zero_adj[v] = [(to, graph[v] 中的弧下标), ...]。"""
    zero_adj: list[list[tuple[int, int]]] = [[] for _ in graph]
    for v in range(len(graph)):
        for i, e in enumerate(graph[v]):
            if e[2] > 0 and e[3] + potential[v] - potential[e[0]] == 0:
                zero_adj[v].append((e[0], i))
    return zero_adj


def _strong_components(zero_adj: list[list[tuple[int, int]]]) -> list[int]:
    """迭代式 Kosaraju，返回每个节点所属强连通分量编号（确定性的）。"""
    n = len(zero_adj)
    order: list[int] = []
    visited = [False] * n
    for root in range(n):
        if visited[root]:
            continue
        visited[root] = True
        stack = [(root, 0)]
        while stack:
            v, i = stack[-1]
            if i < len(zero_adj[v]):
                stack[-1] = (v, i + 1)
                w = zero_adj[v][i][0]
                if not visited[w]:
                    visited[w] = True
                    stack.append((w, 0))
            else:
                order.append(v)
                stack.pop()

    rev_adj: list[list[int]] = [[] for _ in range(n)]
    for v in range(n):
        for w, _ in zero_adj[v]:
            rev_adj[w].append(v)

    comp = [-1] * n
    label = 0
    for root in reversed(order):
        if comp[root] != -1:
            continue
        comp[root] = label
        stack = [root]
        while stack:
            v = stack.pop()
            for w in rev_adj[v]:
                if comp[w] == -1:
                    comp[w] = label
                    stack.append(w)
        label += 1
    return comp


def _find_forward_arc(
    graph: list[list[list]], u: int, seg_pos: int
) -> tuple[int, list]:
    """定位 segments[seg_pos] 从 u 出发的正向弧（用于交换环推流）。"""
    for i, e in enumerate(graph[u]):
        if e[4] == seg_pos:
            return i, e
    raise AssertionError("正向弧缺失")


def _push_cycle(graph: list[list[list]], cycle_arcs: list[tuple[int, int]]) -> None:
    """沿残余环 (节点, 弧下标) 序列推 1 单位流量（单位容量，环上各弧均可行）。"""
    for v, i in cycle_arcs:
        e = graph[v][i]
        assert e[2] > 0, "交换环上的弧必须有残余容量"
        e[2] -= 1
        graph[e[0]][e[1]][2] += 1


def _collapse_trail(
    segments: list[Segment],
    trail: list[int],
    index_of: dict[str, int],
    s: int,
    t: int,
) -> list[int]:
    """把可能夹带环流的 s->t 行走轨迹压缩成一条简单路径。

    再次走到已出现过的节点时，两次出现之间的边构成环流，整段剥除
    （这些边不属于任何 s-t 路径）。
    """
    chain: list[int] = []
    pos_of: dict[int, int] = {s: 0}  # 节点 -> 离开该节点的边在 chain 中的位置
    for pos in trail:
        chain.append(pos)
        w = index_of[segments[pos].dst]
        if w == t:
            break
        if w in pos_of:
            cut = pos_of[w]
            for p in chain[cut:]:
                pos_of.pop(index_of[segments[p].dst], None)
            chain = chain[:cut]
        pos_of[w] = len(chain)
    assert chain and index_of[segments[chain[-1]].dst] == t
    return chain


def _decompose_flow(
    segments: list[Segment],
    forward_edges: list[list],
    index_of: dict[str, int],
    s: int,
    t: int,
) -> list[PathResult]:
    """把值为 2 的整 s-t 流（可能夹带零费用环流）分解为两条简单路径。

    每段容量 1，整流在任意非端点处入流量等于出流量：从起点沿承载
    流量的弧一直走，必到终点；轨迹中夹着的环流用 _collapse_trail
    剥除。未进入两条路径的剩余承载边只可能构成环流（其费用为 0），
    不影响见证：两条路径边互不重复、连续且总费用等于原流总费用。
    """
    active = {
        pos
        for pos in range(len(segments))
        if forward_edges[pos][2] == 0  # 残余容量 0 => 承载 1 单位流量
    }
    outgoing: dict[int, list[int]] = {}
    for pos in active:
        outgoing.setdefault(index_of[segments[pos].src], []).append(pos)

    paths: list[PathResult] = []
    for _ in range(REQUIRED_FLOW):
        v = s
        trail: list[int] = []
        while v != t:
            pos = outgoing[v].pop()
            active.discard(pos)
            trail.append(pos)
            v = index_of[segments[pos].dst]
        chain = _collapse_trail(segments, trail, index_of, s, t)
        chosen = [segments[p] for p in chain]
        paths.append(PathResult(chosen, sum(seg.delay for seg in chosen)))
    return paths


def _witness_for(
    segments: list[Segment],
    net,
    zero_adj: list[list[tuple[int, int]]],
    seg_pos: int,
    s: int,
    t: int,
) -> list[PathResult]:
    """为可替换段构造替代见证：沿零费用交换环推流后分解出两条完整链路。

    交换环 = G0 中 u -> v 的零费用路径 + 该段自身的反向弧 v -> u。
    推流后该段流量被置换出去，总费用不变（环费用为 0），得到的新流
    仍是最优二单位流，分解即得两条不含该段、总延迟与基准相同的链路。
    """
    seg = segments[seg_pos]
    u, v = net.index_of[seg.src], net.index_of[seg.dst]

    # 在 G0 中找 u -> v 路径（BFS，弧顺序固定 => 结果确定）。
    # u、v 同强连通分量保证路径存在；该路径不可能用到目标段自身的
    # 反向弧（那要求先到达 u，而 BFS 从 u 出发不再回头）。
    prev: dict[int, tuple[int, int]] = {}
    seen = {u}
    queue = deque([u])
    while queue and v not in seen:
        x = queue.popleft()
        for w, arc_idx in zero_adj[x]:
            if w not in seen:
                seen.add(w)
                prev[w] = (x, arc_idx)
                queue.append(w)
    assert v in prev or v == u, "同强连通分量内必须存在零费用路径"

    # 复制残余容量，在副本上推交换环，避免污染其他段的判定。
    graph = [[arc.copy() for arc in arcs] for arcs in net.graph]
    cycle: list[tuple[int, int]] = []
    node = v
    while node != u:
        px, arc_idx = prev[node]
        cycle.append((px, arc_idx))
        node = px
    cycle.reverse()
    _, fwd = _find_forward_arc(graph, u, seg_pos)
    cycle.append((v, fwd[1]))  # 目标段的反向弧 v -> u
    _push_cycle(graph, cycle)

    # 副本上的正向弧引用（与 net.forward_edges 同位置）。
    copied_forward = []
    for pos, sg in enumerate(segments):
        _, arc = _find_forward_arc(graph, net.index_of[sg.src], pos)
        copied_forward.append(arc)
    assert copied_forward[seg_pos][2] == 1, "交换后目标段必须不再承载流量"

    witness = _decompose_flow(segments, copied_forward, net.index_of, s, t)
    return witness


def audit_segments(
    segments: list[Segment], source: str, target: str
) -> AuditResult:
    """对当前拓扑重求最优二单位流，并逐段审计基准方案中的已用段。

    判定只依赖该最优流残余网络中的零费用交换环（反向弧折算费用为 0
    且两端同强连通分量），不对每个段重新求解规划问题；同一拓扑下
    重复审计结果完全一致。
    """
    net = _build_network(segments, source, target)
    s, t = net.index_of[source], net.index_of[target]

    flow = _min_cost_flow(net.graph, s, t)
    if flow == 0:
        return AuditResult(status="unreachable", source=source, target=target)
    if flow < REQUIRED_FLOW:
        return AuditResult(status="insufficient", source=source, target=target)

    baseline = _decompose_paths(segments, net.forward_edges, net.index_of, s, t)
    total = sum(p.delay for p in baseline)

    potential = _feasible_potentials(net.graph)
    zero_adj = _zero_cost_adjacency(net.graph, potential)
    comp = _strong_components(zero_adj)

    audits: list[SegmentAudit] = []
    for pos, seg in enumerate(segments):
        if net.forward_edges[pos][2] != 0:
            audits.append(
                SegmentAudit(seg, used_in_baseline=False, classification=UNUSED)
            )
            continue
        u, v = net.index_of[seg.src], net.index_of[seg.dst]
        # 该段反向弧的折算费用：为 0 才可能落在零费用交换环上。
        rev = net.graph[v][net.forward_edges[pos][1]]
        rev_rc = rev[3] + potential[v] - potential[u]
        if rev_rc != 0 or comp[u] != comp[v]:
            # 不存在经过该段反向弧的零费用交换环 => 一切最优双路都必须用它
            audits.append(
                SegmentAudit(seg, used_in_baseline=True, classification=MANDATORY)
            )
            continue
        witness = _witness_for(segments, net, zero_adj, pos, s, t)
        # 见证自检：绝不允许伪造——必须不含该段、边互不重复、总延迟相同。
        wids = [sg.id for p in witness for sg in p.segments]
        assert seg.id not in wids
        assert len(wids) == len(set(wids))
        assert sum(p.delay for p in witness) == total
        audits.append(
            SegmentAudit(
                seg,
                used_in_baseline=True,
                classification=REPLACEABLE,
                witness=witness,
            )
        )

    return AuditResult(
        status="ok",
        source=source,
        target=target,
        baseline_paths=baseline,
        total_delay=total,
        audits=audits,
    )
