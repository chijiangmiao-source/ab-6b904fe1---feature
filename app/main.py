"""束线保护双路径规划服务：健康入口 + 求解接口 + 静态页面。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .solver import Segment, solve_two_paths
from .validation import validate_payload

BASE_DIR = Path(__file__).resolve().parent.parent

app = FastAPI(title="束线保护双路径规划")


def _segment_json(seg: Segment) -> dict:
    return {"id": seg.id, "from": seg.src, "to": seg.dst, "delay": seg.delay}


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
        "paths": [
            {
                "delay": path.delay,
                "segments": [_segment_json(seg) for seg in path.segments],
            }
            for path in result.paths
        ],
    }


app.mount(
    "/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static"
)
