#!/usr/bin/env python3
"""Campus planning: separate deadlines, verify conflicts, never invent travel times."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TZ = timezone(timedelta(hours=8))


def moment(value):
    if not isinstance(value, str) or len(value) < 16:
        raise ValueError("时间须含日期和时分，例 2026-09-23T09:00+08:00")
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def interval(item):
    start, end = moment(item["start"]), moment(item["end"])
    if end <= start:
        raise ValueError(f"{item.get('id')}: 结束时间必须晚于开始")
    return start, end


def overlap(a, b):
    sa, ea = interval(a)
    sb, eb = interval(b)
    return sa < eb and sb < ea


def expand_courses(data):
    events = [dict(e) for e in data.get("events", [])]
    if not data.get("courses"):
        return events
    anchor = date.fromisoformat(data["semester"]["week1_monday"])
    if anchor.weekday() != 0:
        raise ValueError("week1_monday 必须是周一")
    for course in data["courses"]:
        weekday = course["weekday"]
        if not isinstance(weekday, int) or not 1 <= weekday <= 7:
            raise ValueError("weekday 必须为 1..7")
        weeks = course["weeks"]
        if len(set(weeks)) != len(weeks):
            raise ValueError("课程周次重复")
        for week in weeks:
            if not isinstance(week, int) or not 1 <= week <= 60:
                raise ValueError("weeks 须为明确的 1..60 整数列表")
            day = anchor + timedelta(weeks=week - 1, days=weekday - 1)
            if day.isoformat() in course.get("exclude_dates", []):
                continue
            start = datetime.combine(day, time.fromisoformat(course["start_time"]), TZ)
            end = datetime.combine(day, time.fromisoformat(course["end_time"]), TZ)
            events.append(
                {
                    "id": f"{course['id']}-w{week}",
                    "title": course["title"],
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "location": course.get("location"),
                    "campus": course.get("campus"),
                    "mode": course.get("mode", "physical"),
                    "source": "user_course",
                    "week": week,
                }
            )
    return events


def route_between(a, b, routes, buffer):
    _, ea = interval(a)
    sb, _ = interval(b)
    gap = (sb - ea).total_seconds() / 60
    result = {
        "from": a["id"],
        "to": b["id"],
        "gap_minutes": gap,
        "travel_minutes": None,
        "buffer_minutes": buffer,
    }
    if gap < 0:
        return {**result, "status": "overlap"}
    if a.get("mode") == "online" or b.get("mode") == "online":
        return {
            **result,
            "status": "online_location_unconfirmed",
            "note": "线上活动仍可能占用具体位置，未据此假定跨校区交通为零。",
        }
    loc_a, loc_b = a.get("location"), b.get("location")
    ca, cb = a.get("campus"), b.get("campus")
    if not loc_a or not loc_b:
        return {**result, "status": "location_unknown"}
    same = bool(ca and cb and ca == cb and loc_a == loc_b)
    route = next(
        (
            r
            for r in routes
            if r["from"] == loc_a
            and r["to"] == loc_b
            and r.get("from_campus") == ca
            and r.get("to_campus") == cb
        ),
        None,
    )
    if same:
        minutes = 0
        source = "same_confirmed_location"
    elif route:
        minutes = route["minutes"]
        source = route.get("source")
        if (
            not isinstance(minutes, (int, float))
            or isinstance(minutes, bool)
            or not math.isfinite(minutes)
            or minutes < 0
            or not source
        ):
            raise ValueError("交通参考必须有有限非负分钟数、方向、校区与来源")
        valid = route.get("valid_on")
        if valid and valid != sb.date().isoformat():
            return {**result, "status": "route_outside_date", "source": source}
    else:
        return {
            **result,
            "status": "cross_campus_unknown"
            if ca and cb and ca != cb
            else "travel_unknown",
        }
    return {
        **result,
        "travel_minutes": minutes,
        "source": source,
        "status": "insufficient_buffer"
        if gap < minutes + buffer
        else "sufficient_with_supplied_reference",
    }


def recommend(flexible, events, routes, buffer):
    duration = flexible["duration_minutes"]
    if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
        raise ValueError("duration_minutes 必须是正整数")
    candidates = []
    steps = 0
    for window in flexible["windows"]:
        ws, we = interval(window)
        cursor = ws
        while cursor + timedelta(minutes=duration) <= we:
            steps += 1
            if steps > 10000:
                raise ValueError("办理窗口过大，请缩小日期范围")
            cand = {
                "id": flexible["id"],
                "title": flexible["title"],
                "start": cursor.isoformat(),
                "end": (cursor + timedelta(minutes=duration)).isoformat(),
                "location": flexible.get("location"),
                "campus": flexible.get("campus"),
                "mode": flexible.get("mode", "physical"),
            }
            if not any(overlap(cand, e) for e in events):
                day_events = [
                    e
                    for e in events
                    if interval(e)[0].date() == cursor.date()
                    or interval(e)[1].date() == cursor.date()
                ]
                before = [e for e in day_events if interval(e)[1] <= cursor]
                after = [e for e in day_events if interval(e)[0] >= interval(cand)[1]]
                transitions = []
                if before:
                    transitions.append(
                        route_between(
                            max(before, key=lambda e: interval(e)[1]),
                            cand,
                            routes,
                            buffer,
                        )
                    )
                if after:
                    transitions.append(
                        route_between(
                            cand,
                            min(after, key=lambda e: interval(e)[0]),
                            routes,
                            buffer,
                        )
                    )
                if not any(t["status"] == "insufficient_buffer" for t in transitions):
                    missing_location = cand["mode"] == "physical" and (
                        not cand["campus"] or not cand["location"]
                    )
                    unknown = missing_location or any(
                        t["status"] != "sufficient_with_supplied_reference"
                        for t in transitions
                    )
                    travel = sum(t["travel_minutes"] or 0 for t in transitions)
                    candidates.append(
                        {
                            **cand,
                            "status": "time_fits_travel_unverified"
                            if unknown
                            else "fits_supplied_constraints",
                            "transitions": transitions,
                            "score": [int(unknown), travel, cursor.isoformat()],
                        }
                    )
            cursor += timedelta(minutes=5)
    candidates.sort(key=lambda c: c["score"])
    selected = []
    for cand in candidates:
        if not any(overlap(cand, x) for x in selected):
            selected.append(cand)
        if len(selected) == 3:
            break
    return {
        "id": flexible["id"],
        "status": "proposals_only" if selected else "no_feasible_slot",
        "alternatives": selected,
        "ranking": "交通已知优先；已知交通时长较少优先；再按开始时间。未知交通不当作零耗时。",
        "note": "备选是互斥方案；未写入正式日程。",
    }


def plan(data):
    if data.get("timezone", "Asia/Shanghai") != "Asia/Shanghai":
        raise ValueError("校园日程使用 Asia/Shanghai；其他时区请以带偏移的时间输入")
    events = expand_courses(data)
    deadlines = [dict(d) for d in data.get("deadlines", [])]
    routes = data.get("routes", [])
    buffer = data.get("arrival_buffer_minutes", 0)
    if not isinstance(buffer, int) or isinstance(buffer, bool) or buffer < 0:
        raise ValueError("arrival_buffer_minutes 必须是非负整数")
    ids = set()
    for item in events + deadlines + data.get("flexible", []):
        if (
            not isinstance(item.get("id"), str)
            or not item["id"]
            or not isinstance(item.get("title"), str)
            or not item["title"].strip()
        ):
            raise ValueError("每条记录必须有非空稳定 id 和 title")
        if item["id"] in ids:
            raise ValueError("重复 id: " + item["id"])
        ids.add(item["id"])
    for event in events:
        interval(event)
    for d in deadlines:
        if d.get("location") or d.get("start") or d.get("end"):
            raise ValueError("DDL 不得带地点或占用时间段；实际写作时间另列 event")
        if d.get("precision", "minute") == "date":
            date.fromisoformat(d["due"])
        else:
            moment(d["due"])
    events.sort(key=lambda e: (interval(e)[0], e["id"]))
    conflicts = []
    for i, a in enumerate(events):
        for b in events[i + 1 :]:
            if overlap(a, b):
                conflicts.append({"type": "time_overlap", "items": [a["id"], b["id"]]})
    transitions = []
    for b in events:
        candidates = [
            a
            for a in events
            if a["id"] != b["id"]
            and interval(a)[1] <= interval(b)[0]
            and interval(a)[1].date() == interval(b)[0].date()
        ]
        if not candidates:
            continue
        latest = max(interval(a)[1] for a in candidates)
        for a in candidates:
            if interval(a)[1] == latest:
                t = route_between(a, b, routes, buffer)
                transitions.append(t)
                if t["status"] == "insufficient_buffer":
                    conflicts.append(
                        {
                            "type": "travel_buffer",
                            "items": [a["id"], b["id"]],
                            "detail": t,
                        }
                    )
    incomplete = [
        e["id"]
        for e in events
        if e.get("mode", "physical") == "physical"
        and (not e.get("campus") or not e.get("location"))
    ]
    unknown = bool(incomplete) or any(
        t["status"] not in {"sufficient_with_supplied_reference", "insufficient_buffer"}
        for t in transitions
    )
    proposals = [recommend(f, events, routes, buffer) for f in data.get("flexible", [])]
    return {
        "status": "ok",
        "planning_status": "conflicts_found"
        if conflicts
        else "travel_unverified"
        if unknown
        else "consistent_with_supplied_constraints",
        "timezone": "Asia/Shanghai",
        "events": events,
        "deadlines": deadlines,
        "conflicts": conflicts,
        "transitions": transitions,
        "incomplete_location_ids": incomplete,
        "flexible_proposals": proposals,
        "notes": [
            "仅检查提供的数据，不代表校园信息完整。",
            "截止日期未占用时间；未创建外部日历、预约或自动提醒。",
            "课程节次须先核对作息表；节假日与调课仅按输入的例外日期处理。",
            f"采用用户输入的到场缓冲 {buffer} 分钟；未提供时为 0，表示尚未设置。",
            "各项弹性任务分别推荐；选择后须合并为固定事项重新检查互相冲突。",
        ],
    }


def escape(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def folded(line):
    result = []
    current = ""
    size = 0
    for c in line:
        n = len(c.encode("utf-8"))
        if size + n > 75:
            result.append(current)
            current = " "
            size = 1
        current += c
        size += n
    result.append(current)
    return "\r\n".join(result)


def stamp(value):
    return moment(value).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def calendar(result):
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//LSNU Campus Assistant//Plan Draft//ZH",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:乐师日程草稿",
    ]
    for kind, items in [("VEVENT", result["events"]), ("VTODO", result["deadlines"])]:
        for item in items:
            uid = (
                hashlib.sha256((kind + ":" + item["id"]).encode()).hexdigest()[:32]
                + "@lsnu-compus-skill.local"
            )
            lines += [
                "BEGIN:" + kind,
                "UID:" + uid,
                "DTSTAMP:" + now,
                "SUMMARY:" + escape(item["title"]),
                "DESCRIPTION:"
                + escape("用户提供的日程草稿；状态：" + result["planning_status"]),
            ]
            if kind == "VEVENT":
                lines += [
                    "DTSTART:" + stamp(item["start"]),
                    "DTEND:" + stamp(item["end"]),
                    "STATUS:TENTATIVE",
                ]
                if item.get("location"):
                    lines.append("LOCATION:" + escape(item["location"]))
            else:
                lines.append(
                    "DUE;VALUE=DATE:"
                    + date.fromisoformat(item["due"]).strftime("%Y%m%d")
                    if item.get("precision") == "date"
                    else "DUE:" + stamp(item["due"])
                )
                lines.append("STATUS:NEEDS-ACTION")
            if "alarm_minutes" in item:
                minutes = item["alarm_minutes"]
                if (
                    not isinstance(minutes, int)
                    or isinstance(minutes, bool)
                    or minutes < 0
                ):
                    raise ValueError("alarm_minutes 须为非负整数")
                if item.get("precision") == "date":
                    raise ValueError("日期级截止尚无时刻，不生成精确提醒")
                lines += [
                    "BEGIN:VALARM",
                    "ACTION:DISPLAY",
                    "DESCRIPTION:" + escape(item["title"]),
                    "TRIGGER;RELATED="
                    + ("END" if kind == "VTODO" else "START")
                    + ":-PT"
                    + str(minutes)
                    + "M",
                    "END:VALARM",
                ]
            lines.append("END:" + kind)
    lines.append("END:VCALENDAR")
    return "\r\n".join(folded(line) for line in lines) + "\r\n"


def save_external(path, text, overwrite=False):
    target = Path(path).expanduser().resolve()
    if target.is_relative_to(ROOT):
        raise ValueError("个人输出必须位于 Skill／仓库目录之外")
    if target.exists() and not overwrite:
        raise ValueError("输出已存在；另选文件名，或明确使用 --overwrite")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=target.parent, delete=False
    ) as f:
        f.write(text)
        temp = Path(f.name)
    temp.replace(target)
    return str(target)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input")
    p.add_argument("--output")
    p.add_argument("--ics")
    p.add_argument("--overwrite", action="store_true")
    args = p.parse_args()
    try:
        data = json.loads(Path(args.input).read_text(encoding="utf-8"))
        result = plan(data)
        ics = calendar(result) if args.ics else None
        if args.ics:
            result["ics_file"] = save_external(args.ics, ics, args.overwrite)
        payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            save_external(args.output, payload, args.overwrite)
        print(payload)
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(
            json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False),
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
