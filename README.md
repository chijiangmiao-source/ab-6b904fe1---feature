# 束线保护信号接入 · 双路径规划

录入有向光纤段（段标识、起点、终点、非负整数延迟），经真实 HTTP 接口求出
起点到终点 **两条边互不重复、总延迟最小** 的保护链路；无法形成双路时返回
最小割的源侧节点集合与全部外出割边作为瓶颈证据。

## 算法

将每段光纤建模为容量 1、费用为延迟的有向弧，求流量 2 的**最小费用流**
（连续最短路 SSP + Johnson 势函数 + Dijkstra），再分解为两条路径。

- 保证**全局最优**：不是"先求一条最短路再删边"的贪心。贪心在局部最短路
  占用共享光纤时会误判无解；最小费用流会在整体上重选路径组合。
- 节点允许重合，允许并行光纤（同端点多条不同标识的段各占 1 单位容量）。
- 最大流不足 2 时，在残余网络上从起点做可达搜索：可达集合即最小割源侧
  集合，由该集合指向外部的全部原始段即所有外出割边。

## 运行

```bash
# 宿主机端口可用 HOST_PORT 配置（默认 8080）
HOST_PORT=8080 docker compose up --build app
# 浏览器访问 http://localhost:8080 ，健康入口 GET /health
```

## 一键验收（Compose verify）

```bash
docker compose up --build --abort-on-container-exit --exit-code-from verify verify
# 或： docker compose run --build verify
```

`verify` 服务会依次执行并以退出码报告结果（全过为 0）：

1. 构建检查（字节码编译）；
2. `pytest`：求解器（含 4000 个随机有向图对全枚举蛮力的对拍）、输入校验、HTTP 接口；
3. API/HTTP 冒烟与业务验收（`verify/acceptance.py`，对真实运行中的服务发请求）：
   - 贪心反例必须返回全局最优总延迟 12，并用**独立枚举**复核；
     校验两条路径边互不重复、链路连续、各路径延迟与总延迟均可复算；
   - 共享瓶颈：源侧集合 + 全部外出割边，并用独立最大流确认双路不存在、
     移除割边后路径数为 0；
   - 负延迟、重复段标识、不存在端点、不可达输入均返回 400 且错误定位到具体字段；
   - 并行光纤/零延迟、静态页面与过期请求防护的冒烟。

## 本地无 Docker 时

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080 &
.venv/bin/python -m pytest -q
.venv/bin/python verify/acceptance.py --base-url http://localhost:8080
```

## 接口

`POST /api/protected-paths`

```json
{
  "source": "S", "target": "T",
  "segments": [
    {"id": "e1", "from": "S", "to": "A", "delay": 1}
  ]
}
```

- 成功：`{"status":"ok","totalDelay":12,"paths":[{"delay":6,"segments":[...]}, ...]}`
- 无法形成双路：`{"status":"insufficient","cut":{"sourceSet":[...],"edges":[...]}}`
- 输入错误（负延迟/重复标识/端点不存在）或不可达：HTTP 400，
  `errors` 中每项带 `loc`（如 `segments[2].delay`）定位字段。

## 前端行为

- 所有结果只来自真实接口；展示每条链路段标识、逐段延迟、路径延迟与
  可复算的最小总延迟（页面同时显示加法复算式）。
- 提交即清除旧结论；请求带单调序号并使用 `AbortController`，
  过期响应不会覆盖新草稿。
