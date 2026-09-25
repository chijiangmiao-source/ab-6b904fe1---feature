"""验收脚本：通过真实 HTTP 接口校验业务结果。

校验内容：
  1. 健康入口；
  2. 双路：贪心反例必须返回全局最优 12（用独立枚举对拍，而非信任求解器），
     两条路径边互不重复、链路连续、总延迟可复算；
  3. 共享瓶颈：返回源侧节点集合与**全部**外出割边，并独立验证割的容量与阻断性；
  4. 负延迟/重复段标识/不存在端点/不可达均定位报错；
  5. 并行光纤/零延迟、静态页面与过期请求防护的冒烟；
  6. 最优段审计：必经段判定（零见证）、可替换段的等延迟双路见证、
     按段复算一致性、重复审计稳定性、非法输入定位、独立枚举对拍。

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

# 审计用例：已用段 x(A->B,2) 可被等延迟绕行 A->C->B(1+1) 替换；
# e1..e4 是全部最优方案共有的必经段；e5/e6 基准未选用。
AUDIT_CASE = {
    "source": "S",
    "target": "T",
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
    check("结果页可发起最优段审计",
          "segment-audit" in html and "发起最优段审计" in html)
    check("审计区含必经/可替换并列复核",
          "必经段" in html and "可替换段" in html and "基准方案" in html)


def _audit_request(base_url: str, payload, segment_id=None):
    body = dict(payload)
    if segment_id is not None:
        body["segmentId"] = segment_id
    return _post(base_url, "/api/segment-audit", body)


def _post(base_url: str, path: str, payload):
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


def _optimum_pair_sets(segments, source, target):
    """独立枚举：返回 (最小总延迟, {最优路径对 frozenset({id,..}), frozenset(...)}）。"""
    adj = {}
    for s in segments:
        adj.setdefault(s["from"], []).append((s["to"], s))
    paths = []

    def dfs(node, used, used_nodes, cost):
        if node == target:
            paths.append((frozenset(used), cost))
            return
        for nxt, s in adj.get(node, []):
            if s["id"] not in used and nxt not in used_nodes:
                dfs(nxt, used | {s["id"]}, used_nodes | {nxt}, cost + s["delay"])

    dfs(source, frozenset(), {source}, 0)
    best, best_sets = None, set()
    for (p1, c1), (p2, c2) in itertools.combinations_with_replacement(paths, 2):
        if p1.isdisjoint(p2):
            cand = c1 + c2
            if best is None or cand < best:
                best, best_sets = cand, {frozenset(p1 | p2)}
            elif cand == best:
                best_sets.add(frozenset(p1 | p2))
    return best, best_sets


def _validate_witness(entry, baseline_total, source, target):
    """复核单段见证：两条完整链路、连续、边互不重复、不含该段、总延迟相同。"""
    seg_id = entry["id"]
    witness = entry.get("witness")
    if not (isinstance(witness, list) and len(witness) == 2):
        return False, "witness 必须是两条链路"
    used = []
    for p in witness:
        chain = p["segments"]
        if not chain or chain[0]["from"] != source or chain[-1]["to"] != target:
            return False, f"{seg_id} 见证链路非 {source}->{target}"
        for a, b in zip(chain, chain[1:]):
            if a["to"] != b["from"]:
                return False, f"{seg_id} 见证链路不连续"
        if p["delay"] != sum(s["delay"] for s in chain):
            return False, f"{seg_id} 见证路径延迟不可复算"
        used.extend(s["id"] for s in chain)
    if seg_id in used:
        return False, f"{seg_id} 见证仍含被审计段"
    if len(used) != len(set(used)):
        return False, f"{seg_id} 见证两条路径边重复"
    if sum(p["delay"] for p in witness) != baseline_total:
        return False, f"{seg_id} 见证总延迟与基准不同"
    return True, ""


def verify_segment_audit(base_url: str) -> None:
    print("\n== 5. 最优段审计（零费用交换环判定 + 等延迟替代见证） ==")
    status, data = _post(base_url, "/api/segment-audit", AUDIT_CASE)
    check("审计 HTTP 200", status == 200, str(data))
    check("审计 status=ok", data.get("status") == "ok", str(data))
    check("审计基准总延迟 = 2", data.get("totalDelay") == 2, str(data.get("totalDelay")))

    baseline = data.get("baseline", [])
    check("基准返回两条完整链路", len(baseline) == 2, str(baseline))
    base_ids = {s["id"] for p in baseline for s in p["segments"]}
    check("基准边集 = {e1,x,e2,e3,e4}",
          base_ids == {"e1", "x", "e2", "e3", "e4"}, str(base_ids))
    check("基准总延迟可复算",
          sum(s["delay"] for p in baseline for s in p["segments"]) == 2)

    entries = {s["id"]: s for s in data.get("segments", [])}
    check("逐段覆盖拓扑全部 7 段",
          set(entries) == {f"e{i}" for i in range(1, 7)} | {"x"}, str(set(entries)))

    # 独立枚举口径对拍：必经 = 出现在全部最优对；可替换 = 存在不含它的最优对。
    best, best_sets = _optimum_pair_sets(AUDIT_CASE["segments"], "S", "T")
    check("独立枚举确认最小总延迟 = 2", best == 2, f"枚举得 {best}")
    check("存在两个等价最优方案（经 x 与经 e5/e6 绕行）",
          len(best_sets) == 2, f"{[set(x) for x in best_sets]}")
    for sid, e in entries.items():
        in_all = all(sid in pair for pair in best_sets)
        in_base = sid in base_ids
        if not in_base:
            want = "unused"
        elif in_all:
            want = "mandatory"
        else:
            want = "replaceable"
        check(f"段 {sid} 归类 = {want}（独立枚举口径）",
              e["classification"] == want, str(e["classification"]))
        if want == "mandatory":
            check(f"必经段 {sid} 不伪造见证（witness=null）", e["witness"] is None)
        elif want == "replaceable":
            ok, detail = _validate_witness(e, data["totalDelay"], "S", "T")
            check(f"可替换段 {sid} 见证为等延迟合法双路", ok, detail)
            wids = {z["id"] for p in e["witness"] for z in p["segments"]}
            check(f"可替换段 {sid} 见证走绕行 e5/e6", {"e5", "e6"} <= wids, str(wids))

    # 按段复算：结论必须与整表完全一致。
    for sid in ("x", "e1", "e5"):
        st, one = _audit_request(base_url, AUDIT_CASE, sid)
        check(f"按段复算 {sid} HTTP 200", st == 200, str(one))
        seg_list = one.get("segments", [])
        check(f"按段复算 {sid} 仅返回该段", len(seg_list) == 1 and seg_list[0]["id"] == sid)
        if st == 200 and seg_list:
            got = seg_list[0]
            check(f"按段复算 {sid} 归类/见证与整表一致",
                  got == entries[sid], f"{got} != {entries[sid]}")

    # 重复查看同一段：归类与见证保持一致（确定性）。
    st, repeat1 = _audit_request(base_url, AUDIT_CASE, "x")
    st2, repeat2 = _audit_request(base_url, AUDIT_CASE, "x")
    check("重复审计响应完全一致", st == st2 == 200 and repeat1 == repeat2)

    # 不存在的段标识 / 非法输入 / 无双路，均 400 且定位字段。
    st, d = _audit_request(base_url, AUDIT_CASE, "missing-id")
    check("审计未知段标识返回 400 并定位 segmentId",
          st == 400 and any(e["loc"] == "segmentId" for e in d.get("errors", [])), str(d))
    bad = json.loads(json.dumps(AUDIT_CASE))
    bad["segments"][0]["delay"] = -5
    st, d = _post(base_url, "/api/segment-audit", bad)
    check("审计负延迟 400 定位",
          st == 400 and any(e["loc"] == "segments[0].delay" for e in d.get("errors", [])))
    only_one = {
        "source": "S", "target": "T",
        "segments": [{"id": "a", "from": "S", "to": "T", "delay": 1}],
    }
    st, d = _post(base_url, "/api/segment-audit", only_one)
    check("无双路拓扑审计返回 400", st == 400, str(d))

    # 零延迟并行段冒烟：两条零延迟并行段皆必经，无伪造见证。
    zero_parallel = {
        "source": "S", "target": "T",
        "segments": [
            {"id": "z1", "from": "S", "to": "T", "delay": 0},
            {"id": "z2", "from": "S", "to": "T", "delay": 0},
        ],
    }
    st, d = _post(base_url, "/api/segment-audit", zero_parallel)
    check("零延迟并行段审计 200", st == 200, str(d))
    if st == 200:
        check("两条零延迟并行段均为必经段",
              all(s["classification"] == "mandatory" and s["witness"] is None
                  for s in d["segments"]), str(d["segments"]))


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
