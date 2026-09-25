"""验收脚本：通过真实 HTTP 接口校验业务结果。

校验内容：
  1. 健康入口；
  2. 双路：贪心反例必须返回全局最优 12（用独立枚举对拍，而非信任求解器），
     两条路径边互不重复、链路连续、总延迟可复算；
  3. 共享瓶颈：返回源侧节点集合与**全部**外出割边，并独立验证割的容量与阻断性；
  4. 负延迟/重复段标识/不存在端点/不可达均定位报错；
  5. 并行光纤与零延迟；静态页面可访问；
  6. 最优段审计：唯一最优下已用段判为必经段且不伪造见证；多等价最优下
     可替换段的替代见证独立复核（完整双路、边互不重复、不含被审段、
     总延迟与基准相同）；按段复算与重复审计结果一致。

任何一项失败即以非零退出码退出。
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
import urllib.error
import urllib.request

GREEDY_CASE = {
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

BOTTLENECK_CASE = {
    "source": "S",
    "target": "T",
    "segments": [
        {"id": "e1", "from": "S", "to": "A", "delay": 2},
        {"id": "e2", "from": "S", "to": "B", "delay": 3},
        {"id": "e3", "from": "A", "to": "X", "delay": 4},
        {"id": "e4", "from": "B", "to": "X", "delay": 5},
        {"id": "e5", "from": "X", "to": "T", "delay": 1},
    ],
}

# 三条等费路由：最优双路不唯一，基准方案的每条已用段都可被等费替代。
MULTI_OPTIMA_CASE = {
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

# 零延迟 + 并行段：p1(0) 为必经段，p2(3) 可由并行段 p3(3) 等费替换。
MIXED_CASE = {
    "source": "S",
    "target": "T",
    "segments": [
        {"id": "p1", "from": "S", "to": "T", "delay": 0},
        {"id": "p2", "from": "S", "to": "T", "delay": 3},
        {"id": "p3", "from": "S", "to": "T", "delay": 3},
    ],
}

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def post(base_url: str, path: str, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        base_url + path, data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def request(base_url: str, payload):
    return post(base_url, "/api/protected-paths", payload)


def audit_request(base_url: str, payload):
    return post(base_url, "/api/segment-audit", payload)


def get(base_url: str, path: str):
    with urllib.request.urlopen(base_url + path, timeout=10) as resp:
        return resp.status, resp.read().decode()


def wait_for_health(base_url: str, attempts: int = 30) -> bool:
    for i in range(attempts):
        try:
            status, _ = get(base_url, "/health")
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def independent_optimum(segments, source, target):
    """独立参考实现：枚举全部简单路径及两两不相交组合的最小总延迟。"""
    adj = {}
    for i, s in enumerate(segments):
        adj.setdefault(s["from"], []).append((s["to"], i))
    paths = []

    def dfs(node, used_edges, used_nodes, cost):
        if node == target:
            paths.append((frozenset(used_edges), cost))
            return
        for nxt, ei in adj.get(node, []):
            if ei not in used_edges and nxt not in used_nodes:
                dfs(nxt, used_edges | {ei}, used_nodes | {nxt},
                    cost + segments[ei]["delay"])

    dfs(source, frozenset(), {source}, 0)
    best = None
    for (p1, c1), (p2, c2) in itertools.combinations_with_replacement(paths, 2):
        if p1.isdisjoint(p2):
            cand = c1 + c2
            best = cand if best is None else min(best, cand)
    return best


def edge_disjoint_path_count(segments, source, target):
    """独立 Ford-Fulkerson（BFS 增广，单位容量，支持并行段）。"""
    adj = {}
    for i, s in enumerate(segments):
        adj.setdefault(s["from"], []).append([s["to"], i, True])
        # 反向引用由增广时翻转，简化为残差邻接表：
    # 用显式残差边结构重做：
    graph = {}

    def edges_of(node):
        return graph.setdefault(node, [])

    edge_refs = []
    for s in segments:
        fwd = [s["to"], 1, None]
        rev = [s["from"], 0, None]
        fwd[2] = rev
        rev[2] = fwd
        edges_of(s["from"]).append(fwd)
        edges_of(s["to"]).append(rev)
        edge_refs.append((fwd, rev))

    count = 0
    while True:
        prev = {source: None}
        queue = [source]
        found = False
        while queue and not found:
            v = queue.pop(0)
            for e in edges_of(v):
                if e[1] > 0 and e[0] not in prev:
                    prev[e[0]] = (v, e)
                    if e[0] == target:
                        found = True
                        break
                    queue.append(e[0])
        if not found:
            break
        node = target
        while node != source:
            v, e = prev[node]
            e[1] -= 1
            e[2][1] += 1
            node = v
        count += 1
    return count


def verify_dual_paths(base_url: str) -> None:
    print("\n== 1. 双路全局最优（贪心反例） ==")
    status, data = request(base_url, GREEDY_CASE)
    check("HTTP 200", status == 200, f"got {status} {data}")
    check("status=ok", data.get("status") == "ok", str(data))
    paths = data.get("paths", [])
    check("返回两条路径", len(paths) == 2, str(paths))

    all_ids, contiguous = [], True
    source, target = GREEDY_CASE["source"], GREEDY_CASE["target"]
    for p in paths:
        segs = p["segments"]
        all_ids.extend(s["id"] for s in segs)
        if not segs or segs[0]["from"] != source or segs[-1]["to"] != target:
            contiguous = False
        for a, b in zip(segs, segs[1:]):
            if a["to"] != b["from"]:
                contiguous = False
        recomputed = sum(s["delay"] for s in segs)
        check(f"路径 {p} 延迟可复算", recomputed == p["delay"],
              f"{recomputed} != {p.get('delay')}")
    check("两条路径边互不重复", len(all_ids) == len(set(all_ids)), str(all_ids))
    check("每条路径为起点到终点的连续链路", contiguous)

    total = data.get("totalDelay")
    check("最小总延迟 = 12（全局最优，非贪心删边结果）", total == 12, str(total))
    check("总延迟 = 两路径延迟之和",
          total == sum(p["delay"] for p in paths), str(total))

    expected = independent_optimum(GREEDY_CASE["segments"], source, target)
    check("独立枚举对拍确认 12 确为全局最优", expected == 12, f"枚举得 {expected}")
    check("接口结果与独立枚举一致", total == expected)


def verify_cut_evidence(base_url: str) -> None:
    print("\n== 2. 共享瓶颈与割证据 ==")
    status, data = request(base_url, BOTTLENECK_CASE)
    check("HTTP 200", status == 200, f"got {status} {data}")
    check("status=insufficient", data.get("status") == "insufficient", str(data))

    cut = data.get("cut", {})
    src_set = set(cut.get("sourceSet", []))
    cut_edges = cut.get("edges", [])
    segments = BOTTLENECK_CASE["segments"]
    source, target = "S", "T"

    check("源侧集合含起点", source in src_set, str(src_set))
    check("源侧集合不含终点", target not in src_set, str(src_set))
    check("源侧集合包含汇聚节点 S,A,B",
          {"S", "A", "B"} <= src_set, str(src_set))

    crossing = {
        s["id"] for s in segments
        if s["from"] in src_set and s["to"] not in src_set
    }
    reported = {e["id"] for e in cut_edges}
    check("每条报告割边均跨源侧/外部集合", reported <= crossing, str(reported))
    check("列出全部外出割边（无遗漏）", reported == crossing,
          f"reported={reported}, actual={crossing}")
    check("外出割边仅 e5（共享光纤瓶颈）", reported == {"e5"}, str(reported))

    # 独立验证：该图边不相交 s-t 路径数确实不足 2；
    # 且移除全部割边后起点不可达终点（割的阻断性）。
    n_paths = edge_disjoint_path_count(segments, source, target)
    check("独立最大流确认双路不存在", n_paths < 2, f"flow={n_paths}")
    kept = [s for s in segments if s["id"] not in reported]
    n_after = edge_disjoint_path_count(kept, source, target)
    check("移除全部外出割边后路径数为 0（割有效）", n_after == 0,
          f"flow after cut={n_after}")


def verify_error_cases(base_url: str) -> None:
    print("\n== 3. 非法输入定位报错 ==")

    bad = {
        "source": "S", "target": "T",
        "segments": [{"id": "e1", "from": "S", "to": "T", "delay": -7}],
    }
    status, data = request(base_url, bad)
    check("负延迟返回 400", status == 400)
    check("负延迟定位到 segments[0].delay",
          any(e["loc"] == "segments[0].delay" for e in data.get("errors", [])),
          str(data))

    dup = {
        "source": "S", "target": "T",
        "segments": [
            {"id": "x", "from": "S", "to": "A", "delay": 1},
            {"id": "x", "from": "A", "to": "T", "delay": 1},
        ],
    }
    status, data = request(base_url, dup)
    check("重复段标识返回 400", status == 400)
    check("重复段标识定位到具体下标",
          any(e["loc"] == "segments[1].id" for e in data.get("errors", [])),
          str(data))

    missing = {
        "source": "Z", "target": "T",
        "segments": [{"id": "e1", "from": "S", "to": "T", "delay": 1}],
    }
    status, data = request(base_url, missing)
    check("不存在端点返回 400", status == 400)
    check("不存在端点定位到 source",
          any(e["loc"] == "source" for e in data.get("errors", [])), str(data))

    unreachable = {
        "source": "S", "target": "T",
        "segments": [
            {"id": "e1", "from": "S", "to": "A", "delay": 1},
            {"id": "e2", "from": "B", "to": "T", "delay": 1},
        ],
    }
    status, data = request(base_url, unreachable)
    check("不可达输入返回 400", status == 400)
    check("不可达定位到 target",
          any(e["loc"] == "target" for e in data.get("errors", [])), str(data))


def check_witness_json(witness, excluded_id, source, target, baseline_total,
                       valid_ids, label) -> None:
    """独立复核替代见证：完整双路、边互不重复、不含被审段、总延迟等于基准。"""
    check(f"{label}：提供替代见证", witness is not None)
    if witness is None:
        return
    check(f"{label}：见证总延迟与基准相同",
          witness.get("totalDelay") == baseline_total,
          f"{witness.get('totalDelay')} != {baseline_total}")
    paths = witness.get("paths", [])
    check(f"{label}：见证为两条完整链路", len(paths) == 2, str(paths))
    ids, contiguous, recompute_ok = [], True, True
    for p in paths:
        segs = p.get("segments", [])
        if not segs or segs[0]["from"] != source or segs[-1]["to"] != target:
            contiguous = False
        for a, b in zip(segs, segs[1:]):
            if a["to"] != b["from"]:
                contiguous = False
        if sum(s["delay"] for s in segs) != p.get("delay"):
            recompute_ok = False
        ids.extend(s["id"] for s in segs)
    check(f"{label}：见证链路为起点到终点的连续链路", contiguous)
    check(f"{label}：见证各链路延迟可复算", recompute_ok)
    check(f"{label}：见证双路边互不重复", len(ids) == len(set(ids)), str(ids))
    check(f"{label}：见证不含被审段 {excluded_id}",
          excluded_id not in ids, str(ids))
    check(f"{label}：见证所用段均来自原始拓扑",
          set(ids) <= valid_ids, str(ids))
    check(f"{label}：见证总延迟 = 两链路延迟之和",
          sum(p.get("delay", -1) for p in paths) == baseline_total)


def verify_segment_audit(base_url: str) -> None:
    print("\n== 5. 最优段审计：必经段判定与可替换见证 ==")

    # 5.1 唯一全局最优（贪心反例）：全部已用段为必经段，且不得伪造见证。
    status, plan = request(base_url, GREEDY_CASE)
    status2, data = audit_request(base_url, GREEDY_CASE)
    check("审计接口 HTTP 200", status == 200 and status2 == 200,
          f"plan={status} audit={status2}")
    check("审计 status=ok", data.get("status") == "ok", str(data))
    check("审计基准方案与规划接口一致",
          data.get("paths") == plan.get("paths")
          and data.get("totalDelay") == plan.get("totalDelay"))
    audits = data.get("audits", [])
    used_ids = {s["id"] for p in plan["paths"] for s in p["segments"]}
    check("审计覆盖基准方案全部已用段",
          {a["segmentId"] for a in audits} == used_ids == {"e1", "e3", "e4", "e5"},
          str(audits))
    for a in audits:
        check(f"唯一最优下段 {a['segmentId']} 判为必经段",
              a["classification"] == "mandatory", str(a))
        check(f"必经段 {a['segmentId']} 不伪造替代见证",
              a["witness"] is None, str(a))

    # 5.2 多个等价最优方案：每条已用段均可替换，见证独立复核。
    expected = independent_optimum(
        MULTI_OPTIMA_CASE["segments"], "S", "T")
    check("独立枚举确认多路由案例最优总延迟为 4", expected == 4,
          f"枚举得 {expected}")
    status, data = audit_request(base_url, MULTI_OPTIMA_CASE)
    check("多最优审计 HTTP 200 且 status=ok",
          status == 200 and data.get("status") == "ok", str(data))
    check("多最优审计基准总延迟 = 4（与独立枚举一致）",
          data.get("totalDelay") == 4, str(data.get("totalDelay")))
    valid_ids = {s["id"] for s in MULTI_OPTIMA_CASE["segments"]}
    audits = data.get("audits", [])
    check("多最优审计覆盖 4 条已用段", len(audits) == 4, str(audits))
    for a in audits:
        check(f"段 {a['segmentId']} 判为可替换",
              a["classification"] == "replaceable", str(a))
        check_witness_json(a.get("witness"), a["segmentId"], "S", "T", 4,
                           valid_ids, f"段 {a['segmentId']} 的见证")

    # 5.3 零延迟段 + 并行段：p1 必经、p2 可替换（见证为 p1+p3，总延迟 3）。
    status, data = audit_request(base_url, MIXED_CASE)
    check("混合案例审计 HTTP 200 且 status=ok",
          status == 200 and data.get("status") == "ok", str(data))
    by_id = {a["segmentId"]: a for a in data.get("audits", [])}
    check("零延迟段 p1 判为必经段且无见证",
          by_id.get("p1", {}).get("classification") == "mandatory"
          and by_id.get("p1", {}).get("witness") is None, str(by_id))
    check("并行段 p2 判为可替换",
          by_id.get("p2", {}).get("classification") == "replaceable", str(by_id))
    check_witness_json(by_id.get("p2", {}).get("witness"), "p2", "S", "T", 3,
                       {"p1", "p2", "p3"}, "p2 的见证")
    w = by_id.get("p2", {}).get("witness") or {}
    w_ids = {s["id"] for p in w.get("paths", []) for s in p["segments"]}
    check("p2 的替代见证恰为 p1+p3", w_ids == {"p1", "p3"}, str(w_ids))

    # 5.4 按段复算：单段审计结果与全量审计完全一致。
    for a in audits:
        status, one = audit_request(
            base_url, {**MULTI_OPTIMA_CASE, "segmentId": a["segmentId"]})
        ok = (status == 200 and one.get("status") == "ok"
              and len(one.get("audits", [])) == 1
              and one["audits"][0] == a)
        check(f"按段复算 {a['segmentId']} 与全量审计一致", ok, str(one))

    # 5.5 重复查看同一段：归类与替代见证保持一致（响应逐字节相同）。
    _, r1 = audit_request(base_url, MULTI_OPTIMA_CASE)
    _, r2 = audit_request(base_url, MULTI_OPTIMA_CASE)
    check("重复审计响应完全一致（确定性）",
          json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True))

    # 5.6 审计接口的非法输入与边界：未知段标识 / 不可达 / 流不足。
    status, err = audit_request(base_url, {**GREEDY_CASE, "segmentId": "zz"})
    check("未知 segmentId 返回 400 且定位 segmentId",
          status == 400
          and any(e["loc"] == "segmentId" for e in err.get("errors", [])),
          f"{status} {err}")
    status, err = audit_request(base_url, {
        "source": "S", "target": "T",
        "segments": [
            {"id": "e1", "from": "S", "to": "A", "delay": 1},
            {"id": "e2", "from": "B", "to": "T", "delay": 1},
        ],
    })
    check("不可达输入审计返回 400 且定位 target",
          status == 400
          and any(e["loc"] == "target" for e in err.get("errors", [])),
          f"{status} {err}")
    status, ins = audit_request(base_url, BOTTLENECK_CASE)
    check("流不足时审计返回 insufficient 与割证据",
          status == 200 and ins.get("status") == "insufficient"
          and {e["id"] for e in ins.get("cut", {}).get("edges", [])} == {"e5"},
          f"{status} {ins}")

    # 5.7 页面含审计入口与防护钩子。
    status, html = get(base_url, "/")
    check("页面含最优段审计入口", status == 200 and "发起最优段审计" in html)
    check("页面含必经段/可替换归类展示", "必经段" in html and "可替换" in html)
    check("页面调用审计接口并支持按段复算",
          "/api/segment-audit" in html and "按段复算" in html)
    check("页面并列对比基准与替代方案（延迟、边集）",
          "基准方案" in html and "替代方案" in html and "边集" in html)
    check("页面编辑即失效防护（编辑清除旧结论并使在途响应作废）",
          "onEdit" in html and "invalidatePending" in html)


def verify_parallel_and_page(base_url: str) -> None:
    print("\n== 4. 并行光纤/零延迟 与 页面/冒烟 ==")
    payload = {
        "source": "S", "target": "T",
        "segments": [
            {"id": "p1", "from": "S", "to": "T", "delay": 0},
            {"id": "p2", "from": "S", "to": "T", "delay": 3},
            {"id": "p3", "from": "S", "to": "T", "delay": 9},
        ],
    }
    status, data = request(base_url, payload)
    check("并行光纤 HTTP 200", status == 200, str(data))
    check("并行光纤取最小两条，总延迟 3", data.get("totalDelay") == 3, str(data))
    ids = sorted(s["id"] for p in data.get("paths", []) for s in p["segments"])
    check("选用 p1/p2 两条不同段", ids == ["p1", "p2"], str(ids))

    status, html = get(base_url, "/")
    check("首页 200", status == 200)
    check("首页为录入页面", "束线保护" in html and "protected-paths" in html)
    check("页面含过期请求防护（请求序号 + AbortController）",
          "requestSeq" in html and "AbortController" in html)
    check("页面在出错/编辑时清除旧结论",
          "clearResult" in html and "旧结论已清除" in html)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://app:8080")
    args = parser.parse_args()

    print(f"等待服务健康：{args.base_url}/health")
    if not wait_for_health(args.base_url):
        print("[FAIL] 健康入口不可达")
        return 1
    print("[PASS] 健康入口 200")

    verify_dual_paths(args.base_url)
    verify_cut_evidence(args.base_url)
    verify_error_cases(args.base_url)
    verify_parallel_and_page(args.base_url)
    verify_segment_audit(args.base_url)

    print("\n" + "=" * 60)
    if failures:
        print(f"验收失败：{len(failures)} 项 -> {failures}")
        return 1
    print("验收全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
