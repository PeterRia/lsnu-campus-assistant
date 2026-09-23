#!/usr/bin/env python3
"""Summarize supplied activity records, preserving participation evidence."""

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

from planner import save_external


def summarize(data, start, end):
    if end < start:
        raise ValueError("结束日早于开始日")
    seen = {}
    records = []
    duplicates = 0
    for r in data["records"]:
        if not r.get("id") or not r.get("activity"):
            raise ValueError("记录缺少稳定 id 或活动名称")
        if r["id"] in seen:
            if seen[r["id"]] != r:
                raise ValueError("同一 id 内容冲突: " + r["id"])
            duplicates += 1
            continue
        seen[r["id"]] = r
        d = date.fromisoformat(r["date"])
        if r["status"] not in {
            "planned",
            "booked",
            "attended",
            "completed",
            "cancelled",
        }:
            raise ValueError("未知状态")
        minutes = r.get("minutes")
        if minutes is not None and (
            not isinstance(minutes, (int, float))
            or isinstance(minutes, bool)
            or not math.isfinite(minutes)
            or minutes < 0
        ):
            raise ValueError("时长须为有限非负数")
        if start <= d <= end:
            records.append(r)
    groups = defaultdict(
        lambda: {"completed_sessions": 0, "confirmed_minutes": 0, "duration_missing": 0}
    )
    completed = [
        r
        for r in records
        if r["status"] == "completed" and r.get("evidence") == "user_confirmed"
    ]
    for r in completed:
        group = groups[r["activity"]]
        group["completed_sessions"] += 1
        if r.get("minutes") is None:
            group["duration_missing"] += 1
        else:
            group["confirmed_minutes"] += r["minutes"]
    return {
        "status": "ok",
        "period": [start.isoformat(), end.isoformat()],
        "records_in_period": len(records),
        "duplicates_ignored": duplicates,
        "completed_sessions": len(completed),
        "confirmed_minutes": sum(g["confirmed_minutes"] for g in groups.values()),
        "activities": dict(groups),
        "attendance_only": sum(r["status"] == "attended" for r in records),
        "not_counted_as_completed": [r["id"] for r in records if r not in completed],
        "notes": [
            "预约和计划不证明实际参加；签到只证明到场。",
            "分钟数只汇总用户确认完成且明确提供时长的记录。",
            "未记录的日期是缺失数据，不等于没有运动；不据此估算热量或评价健康。",
        ],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input")
    p.add_argument("--start", type=date.fromisoformat, required=True)
    p.add_argument("--end", type=date.fromisoformat, required=True)
    p.add_argument("--output")
    a = p.parse_args()
    try:
        r = summarize(
            json.loads(Path(a.input).read_text(encoding="utf-8")), a.start, a.end
        )
        text = json.dumps(r, ensure_ascii=False, indent=2) + "\n"
        if a.output:
            save_external(a.output, text)
        print(text)
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(
            json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False),
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
