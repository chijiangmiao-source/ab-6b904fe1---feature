"""请求体校验：所有错误都必须定位到具体字段，便于页面逐项提示。"""
from __future__ import annotations

from .solver import Segment

Parsed = tuple[list[Segment], str, str]


def validate_payload(data: object) -> tuple[Parsed | None, list[dict]]:
    """校验录入草稿。

    返回 (parsed, errors)：parsed 为 (segments, source, target)；
    errors 中每项形如 {"loc": "segments[2].delay", "message": "..."}。
    """
    if not isinstance(data, dict):
        return None, [{"loc": "body", "message": "请求体必须是 JSON 对象"}]

    errors: list[dict] = []
    raw_source = data.get("source")
    raw_target = data.get("target")
    raw_segments = data.get("segments")

    source = raw_source.strip() if isinstance(raw_source, str) else raw_source
    target = raw_target.strip() if isinstance(raw_target, str) else raw_target

    if not isinstance(source, str) or not source:
        errors.append({"loc": "source", "message": "起点必须是非空字符串"})
    if not isinstance(target, str) or not target:
        errors.append({"loc": "target", "message": "终点必须是非空字符串"})
    if (
        isinstance(source, str)
        and source
        and isinstance(target, str)
        and target
        and source == target
    ):
        errors.append({"loc": "target", "message": "终点不能与起点相同"})

    if not isinstance(raw_segments, list) or not raw_segments:
        errors.append({"loc": "segments", "message": "光纤段列表必须是非空数组"})
        return None, errors

    segments: list[Segment] = []
    endpoints: set[str] = set()
    id_pos: dict[str, int] = {}

    for i, raw in enumerate(raw_segments):
        if not isinstance(raw, dict):
            errors.append({"loc": f"segments[{i}]", "message": "光纤段必须是对象"})
            continue
        seg_id_raw = raw.get("id")
        src_raw = raw.get("from")
        dst_raw = raw.get("to")
        delay = raw.get("delay")
        seg_id = seg_id_raw.strip() if isinstance(seg_id_raw, str) else seg_id_raw
        src = src_raw.strip() if isinstance(src_raw, str) else src_raw
        dst = dst_raw.strip() if isinstance(dst_raw, str) else dst_raw
        ok = True

        if not isinstance(seg_id, str) or not seg_id:
            errors.append(
                {"loc": f"segments[{i}].id", "message": "段标识必须是非空字符串"}
            )
            ok = False
        elif seg_id in id_pos:
            errors.append(
                {
                    "loc": f"segments[{i}].id",
                    "message": f"段标识 '{seg_id}' 与 segments[{id_pos[seg_id]}].id 重复",
                }
            )
            ok = False
        else:
            id_pos[seg_id] = i

        for field_name, value in (("from", src), ("to", dst)):
            if not isinstance(value, str) or not value:
                errors.append(
                    {
                        "loc": f"segments[{i}].{field_name}",
                        "message": "段端点必须是非空字符串",
                    }
                )
                ok = False

        if (
            isinstance(src, str)
            and src
            and isinstance(dst, str)
            and dst
            and src == dst
        ):
            errors.append(
                {"loc": f"segments[{i}].to", "message": "段不能连接同一节点"}
            )
            ok = False

        if isinstance(delay, bool) or not isinstance(delay, int):
            errors.append(
                {
                    "loc": f"segments[{i}].delay",
                    "message": "延迟必须为非负整数",
                }
            )
            ok = False
        elif delay < 0:
            errors.append(
                {
                    "loc": f"segments[{i}].delay",
                    "message": "不允许负延迟，延迟必须为非负整数",
                }
            )
            ok = False

        if ok:
            segments.append(Segment(seg_id, src, dst, delay))
            endpoints.add(src)
            endpoints.add(dst)

    if errors:
        return None, errors

    if isinstance(source, str) and source and source not in endpoints:
        errors.append(
            {
                "loc": "source",
                "message": f"起点 '{source}' 不存在于任何光纤段的端点中",
            }
        )
    if isinstance(target, str) and target and target not in endpoints:
        errors.append(
            {
                "loc": "target",
                "message": f"终点 '{target}' 不存在于任何光纤段的端点中",
            }
        )
    if errors:
        return None, errors

    return (segments, source, target), []
