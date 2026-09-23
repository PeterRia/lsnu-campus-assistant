#!/usr/bin/env python3
"""Audit supplied, explicitly recognized credits; no course-name or grade inference."""

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path


def number(value):
    n = Decimal(str(value))
    if not n.is_finite() or n < 0:
        raise ValueError("学分须为有限非负数")
    return n


def audit(data):
    requirements = data["requirements"]
    if len({r["category"] for r in requirements}) != len(requirements):
        raise ValueError("培养方案类别重复")
    required = {r["category"]: number(r["credits"]) for r in requirements}
    earned = {k: Decimal(0) for k in required}
    unknown = []
    excluded = []
    counted = {}
    for c in data["courses"]:
        if not c.get("course_code"):
            raise ValueError("缺少 course_code；不能只按课程名称去重")
        if c.get("passed") is not True:
            excluded.append({"course_code": c["course_code"], "reason": "未明确通过"})
            continue
        if (
            c.get("recognized") is not True
            or c.get("category") not in required
            or not c.get("recognition_source")
        ):
            unknown.append(
                {
                    "course_code": c["course_code"],
                    "reason": "培养方案归属或认定依据待确认",
                }
            )
            continue
        credits = number(c["credits"])
        identity = (c["category"], credits)
        if c["course_code"] in counted:
            if counted[c["course_code"]] != identity:
                raise ValueError("同一课程的已认定类别或学分冲突: " + c["course_code"])
            excluded.append(
                {
                    "course_code": c["course_code"],
                    "reason": "重修／重复记录不重复计学分",
                }
            )
            continue
        counted[c["course_code"]] = identity
        earned[c["category"]] += credits
    categories = [
        {
            "category": k,
            "required": float(v),
            "recognized_earned": float(earned[k]),
            "remaining": float(max(Decimal(0), v - earned[k])),
        }
        for k, v in required.items()
    ]
    return {
        "status": "needs_review" if unknown else "calculated_from_supplied_records",
        "categories": categories,
        "unknown": unknown,
        "excluded": excluded,
        "counted_courses": list(counted),
        "note": "仅核算用户提供并明确认可的学分类别；不判断毕业或授位资格，未代替学校审核。",
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input")
    a = p.parse_args()
    try:
        print(
            json.dumps(
                audit(json.loads(Path(a.input).read_text(encoding="utf-8"))),
                ensure_ascii=False,
                indent=2,
            )
        )
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation) as e:
        print(
            json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False),
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
