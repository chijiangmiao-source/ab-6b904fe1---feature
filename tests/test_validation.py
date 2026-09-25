"""输入校验测试：负延迟、重复段标识、端点缺失等必须逐字段定位。"""
from __future__ import annotations

import pytest

from app.validation import validate_payload


def base_payload():
    return {
        "source": "S",
        "target": "T",
        "segments": [
            {"id": "e1", "from": "S", "to": "A", "delay": 1},
            {"id": "e2", "from": "A", "to": "T", "delay": 2},
            {"id": "e3", "from": "S", "to": "T", "delay": 9},
        ],
    }


def test_valid_payload_passes():
    parsed, errors = validate_payload(base_payload())
    assert errors == []
    segs, source, target = parsed
    assert source == "S" and target == "T"
    assert [s.id for s in segs] == ["e1", "e2", "e3"]


def test_negative_delay_localized():
    p = base_payload()
    p["segments"][1]["delay"] = -3
    _, errors = validate_payload(p)
    locs = [e["loc"] for e in errors]
    assert "segments[1].delay" in locs


def test_non_integer_delay_localized():
    p = base_payload()
    p["segments"][0]["delay"] = "1.5"
    _, errors = validate_payload(p)
    assert any(e["loc"] == "segments[0].delay" for e in errors)


def test_boolean_delay_rejected():
    p = base_payload()
    p["segments"][0]["delay"] = True  # bool 是 int 子类，必须显式拒绝
    _, errors = validate_payload(p)
    assert any(e["loc"] == "segments[0].delay" for e in errors)


def test_duplicate_segment_id_localized():
    p = base_payload()
    p["segments"][2]["id"] = "e1"
    _, errors = validate_payload(p)
    locs = [e["loc"] for e in errors]
    assert "segments[2].id" in locs
    assert any("重复" in e["message"] for e in errors)


def test_nonexistent_source_and_target_localized():
    p = base_payload()
    p["source"] = "X"
    p["target"] = "Y"
    _, errors = validate_payload(p)
    locs = {e["loc"] for e in errors}
    assert "source" in locs and "target" in locs


def test_self_loop_segment_rejected():
    p = base_payload()
    p["segments"][0]["to"] = "S"
    _, errors = validate_payload(p)
    assert any(e["loc"] == "segments[0].to" for e in errors)


def test_same_source_target_rejected():
    p = base_payload()
    p["target"] = "S"
    _, errors = validate_payload(p)
    assert any(e["loc"] == "target" for e in errors)


def test_empty_segments_rejected():
    p = base_payload()
    p["segments"] = []
    _, errors = validate_payload(p)
    assert any(e["loc"] == "segments" for e in errors)


def test_non_object_body_rejected():
    _, errors = validate_payload(["nope"])
    assert errors and errors[0]["loc"] == "body"
