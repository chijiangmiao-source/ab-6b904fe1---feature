"""验收脚本：通过真实 HTTP 接口校验业务结果。

校验内容：
  1. 健康入口；
  2. 双路：贪心反例必须返回全局最优 12（用独立枚举对拍，而非信任求解器），
     两条路径边互不重复、链路连续、总延迟可复算；
  3. 共享瓶颈：返回源侧节点集合与**全部**外出割边，并独立验证割的容量与阻断性；
  4. 负延迟/重复段标识/不存在端点/不可达均定位报错；
  5. 并行光纤与零延迟；静态页面可访问。

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

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def request(base_url: str, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        base_url + "/api/protected-paths", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


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

    print("\n" + "=" * 60)
    if failures:
        print(f"验收失败：{len(failures)} 项 -> {failures}")
        return 1
    print("验收全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
