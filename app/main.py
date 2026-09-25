"""束线保护双路径规划服务：健康入口 + 求解接口 + 最优段审计接口 + 静态页面。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .audit import MANDATORY, REPLACEABLE, UNUSED, audit_segments
from .solver import Segment, solve_two_paths
from .validation import validate_payload

BASE_DIR = Path(__file__).resolve().parent.parent

app = FastAPI(title="束线保护双路径规划")

_CLASSIFICATION_LABEL = {
    MANDATORY: "必经段",
    REPLACEABLE: "可替换段",
    UNUSED: "未选用",
}


def _segment_json(seg: Segment) -> dict:
    return {"id": seg.id, "from": seg.src, "to": seg.dst, "delay": seg.delay}


def _path_json(path) -> dict:
    return {
        "delay": path.delay,
        "segments": [_segment_json(seg) for seg in path.segments],
    }


def _error_response(loc: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=400, content={"errors": [{"loc": loc, "message": message}]}
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/protected-paths")
async def protected_paths(request: Request):
    try:
        data = await request.json()
    except Exception:
        return _error_response("body", "请求体不是合法 JSON")

    parsed, errors = validate_payload(data)
    if errors:
        return JSONResponse(status_code=400, content={"errors": errors})
    segments, source, target = parsed

    result = solve_two_paths(segments, source, target)

    if result.status == "unreachable":
        return _error_response(
            "target",
            f"从起点 '{source}' 出发无法到达终点 '{target}'，保护链路不可达",
        )

    if result.status == "insufficient":
        return {
            "status": "insufficient",
            "message": "无法形成两条边互不重复的路径，以下为最小割瓶颈证据",
            "cut": {
                "sourceSet": result.cut.source_set,
                "edges": [_segment_json(seg) for seg in result.cut.edges],
            },
        }

    return {
        "status": "ok",
        "totalDelay": result.total_delay,
        "paths": [_path_json(path) for path in result.paths],
    }


@app.post("/api/segment-audit")
async def segment_audit(request: Request):
    """最优段审计：重求最优二单位流作为基准，逐段判定必经/可替换/未选用。

    判定基于最优流残余网络中的零费用交换环（见 app.audit）；可替换段
    附带两条完整替代链路作为见证，必经段的 witness 恒为 null。
    可选字段 segmentId：只返回该段的审计结论（按段复算），归类与见证
    与整表审计完全一致。
    """
    try:
        data = await request.json()
    except Exception:
        return _error_response("body", "请求体不是合法 JSON")

    segment_id = None
    if isinstance(data, dict):
        segment_id = data.get("segmentId")
        if segment_id is not None and (
            not isinstance(segment_id, str) or not segment_id.strip()
        ):
            return _error_response("segmentId", "segmentId 必须是非空字符串")
        if isinstance(segment_id, str):
            segment_id = segment_id.strip()

    parsed, errors = validate_payload(data)
    if errors:
        return JSONResponse(status_code=400, content={"errors": errors})
    segments, source, target = parsed

    result = audit_segments(segments, source, target)

    if result.status == "unreachable":
        return _error_response(
            "target",
            f"从起点 '{source}' 出发无法到达终点 '{target}'，保护链路不可达",
        )
    if result.status == "insufficient":
        return _error_response(
            "segments",
            "当前拓扑无法形成两条边互不重复的路径，请先完成双路规划再发起审计",
        )

    audits = result.audits
    if segment_id is not None:
        audits = [a for a in audits if a.segment.id == segment_id]
        if not audits:
            return _error_response(
                "segmentId", f"段标识 '{segment_id}' 不在本次拓扑中"
            )

    return {
        "status": "ok",
        "source": source,
        "target": target,
        "totalDelay": result.total_delay,
        "baseline": [_path_json(path) for path in result.baseline_paths],
        "segments": [
            {
                "id": a.segment.id,
                "from": a.segment.src,
                "to": a.segment.dst,
                "delay": a.segment.delay,
                "usedInBaseline": a.used_in_baseline,
                "classification": a.classification,
                "label": _CLASSIFICATION_LABEL[a.classification],
                "witness": (
                    [_path_json(path) for path in a.witness]
                    if a.witness is not None
                    else None
                ),
            }
            for a in audits
        ],
    }


app.mount(
    "/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static"
)
