"""束线保护双路径规划服务：健康入口 + 求解接口 + 最优段审计接口 + 静态页面。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .audit import audit_optimal_segments
from .solver import Segment, solve_two_paths
from .validation import validate_payload

BASE_DIR = Path(__file__).resolve().parent.parent

app = FastAPI(title="束线保护双路径规划")


def _segment_json(seg: Segment) -> dict:
    return {"id": seg.id, "from": seg.src, "to": seg.dst, "delay": seg.delay}


def _path_json(path) -> dict:
    return {
        "delay": path.delay,
        "segments": [_segment_json(seg) for seg in path.segments],
    }


def _cut_json(cut) -> dict:
    return {
        "sourceSet": cut.source_set,
        "edges": [_segment_json(seg) for seg in cut.edges],
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
            "cut": _cut_json(result.cut),
        }

    return {
        "status": "ok",
        "totalDelay": result.total_delay,
        "paths": [_path_json(path) for path in result.paths],
    }


@app.post("/api/segment-audit")
async def segment_audit(request: Request):
    """最优段审计：逐段判定基准双路中的已用段是否不可替代。

    请求体与 /api/protected-paths 相同，可选 segmentId 指定单段复算；
    判定基于最优二单位流残量网络中的零费用交换环，可替换段附等费替代见证。
    """
    try:
        data = await request.json()
    except Exception:
        return _error_response("body", "请求体不是合法 JSON")

    segment_id = data.get("segmentId") if isinstance(data, dict) else None

    parsed, errors = validate_payload(data)
    if errors:
        return JSONResponse(status_code=400, content={"errors": errors})
    segments, source, target = parsed

    if segment_id is not None:
        if not isinstance(segment_id, str) or not segment_id.strip():
            return _error_response("segmentId", "segmentId 必须是非空字符串")
        segment_id = segment_id.strip()
        if segment_id not in {seg.id for seg in segments}:
            return _error_response(
                "segmentId", f"段标识 '{segment_id}' 不存在于光纤段列表中"
            )

    result = audit_optimal_segments(segments, source, target, segment_id)

    if result.status == "unreachable":
        return _error_response(
            "target",
            f"从起点 '{source}' 出发无法到达终点 '{target}'，保护链路不可达",
        )

    if result.status == "insufficient":
        return {
            "status": "insufficient",
            "message": "无法形成两条边互不重复的路径，无基准双路可审计；以下为最小割瓶颈证据",
            "cut": _cut_json(result.cut),
        }

    return {
        "status": "ok",
        "totalDelay": result.total_delay,
        "paths": [_path_json(path) for path in result.paths],
        "audits": [
            {
                "segmentId": audit.segment_id,
                "classification": audit.classification,
                "witness": (
                    None
                    if audit.witness is None
                    else {
                        "totalDelay": audit.witness.total_delay,
                        "paths": [
                            _path_json(path) for path in audit.witness.paths
                        ],
                    }
                ),
            }
            for audit in result.audits
        ],
    }


app.mount(
    "/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static"
)
