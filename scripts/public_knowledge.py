"""Validate and retrieve the public observation snapshot produced by GitHub Actions."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from campus import official_url, tokenize

SKILL = "lsnu-campus-skill"
SNAPSHOT_URL = "https://raw.githubusercontent.com/PeterRia/lsnu-campus-skill/public-knowledge/latest.json"
MAX_BYTES = 2 * 1024 * 1024
MAX_AGE = timedelta(hours=3)


def checksum(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def instant(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("采集时间必须带时区")
    return result


def seal(value):
    value = {k: v for k, v in value.items() if k != "snapshot_sha256"}
    return {**value, "snapshot_sha256": checksum(value)}


def validate(value):
    if not isinstance(value, dict):
        raise ValueError("公共信息快照必须是对象")
    if value.get("schema_version") != 1 or value.get("skill_id") != SKILL:
        raise ValueError("公共信息快照标识不匹配")
    if seal(value)["snapshot_sha256"] != value.get("snapshot_sha256"):
        raise ValueError("公共信息快照完整性错误")
    collected = instant(value["collected_at"])
    if collected > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError("采集时间不合理")
    if not isinstance(value.get("items"), list) or len(value["items"]) > 250:
        raise ValueError("公共信息记录数量异常")
    if not isinstance(value.get("feeds"), list) or len(value["feeds"]) > 6:
        raise ValueError("通知入口数量异常")
    seen = set()
    for item in value["items"]:
        if not isinstance(item, dict) or item.get("status") not in {"ok", "error"}:
            raise ValueError("公共来源状态无效")
        if not official_url(item["url"]) or item["url"] in seen:
            raise ValueError("公共来源越界或重复")
        seen.add(item["url"])
        if not isinstance(item["title"], str) or not 1 <= len(item["title"]) <= 300:
            raise ValueError("通知标题无效")
        checked = instant(item["checked_at"])
        if checked > collected:
            raise ValueError("来源检查时间晚于快照")
        if item.get("last_success_at"):
            if instant(item["last_success_at"]) > checked:
                raise ValueError("来源成功时间不合理")
        if item["status"] == "ok" and not item.get("last_success_at"):
            raise ValueError("成功来源缺少成功时间")
        digest = item.get("content_sha256")
        if (digest is not None and not re.fullmatch(r"[0-9a-f]{64}", digest)) or (
            item["status"] == "ok" and digest is None
        ):
            raise ValueError("来源内容指纹无效")
        if item.get("published_on"):
            datetime.strptime(item["published_on"], "%Y-%m-%d")
        if item.get("review_status") != "automatic_observation":
            raise ValueError("自动采集不能声称完成政策审核")
    for feed in value["feeds"]:
        if not isinstance(feed, dict) or not official_url(feed["url"]):
            raise ValueError("通知入口来源越界")
        if feed.get("status") not in {"ok", "error"}:
            raise ValueError("通知入口状态无效")
        if instant(feed["checked_at"]) > collected or (
            feed.get("last_success_at")
            and instant(feed["last_success_at"]) > instant(feed["checked_at"])
        ):
            raise ValueError("通知入口时间不合理")
        if feed["status"] == "ok" and not feed.get("last_success_at"):
            raise ValueError("成功入口缺少成功时间")
        if not isinstance(feed.get("notices"), list) or len(feed["notices"]) > 12:
            raise ValueError("发现通知数量异常")
        if any(not official_url(n["url"]) for n in feed["notices"]):
            raise ValueError("发现通知来源越界")
    revision = checksum(
        [
            {k: x.get(k) for k in ("url", "title", "published_on", "content_sha256")}
            for x in sorted(value["items"], key=lambda x: x["url"])
        ]
    )
    if value.get("content_revision") != revision:
        raise ValueError("公共信息内容版本不匹配")
    return value


def summary(value, now=None):
    now = now or datetime.now(timezone.utc)
    old = now - instant(value["collected_at"]) > MAX_AGE
    failures = sum(
        x["status"] != "ok" for x in [*value["items"], *value.get("feeds", [])]
    )
    return {
        "collected_at": value["collected_at"],
        "content_revision": value["content_revision"],
        "snapshot_sha256": value["snapshot_sha256"],
        "age_seconds": max(
            0, int((now - instant(value["collected_at"])).total_seconds())
        ),
        "freshness": "stale" if old else ("partial" if failures else "fresh"),
        "failed_sources": failures,
        "items": len(value["items"]),
        "note": "采集时间不是政策生效或人工核查时间；标题与指纹仅用于发现变化，关键条款须回读官方原文。",
    }


def sync(cache, fetch):
    # fetch only receives a fixed public URL; no user question or private path.
    from public_update import atomic_json

    path = Path(cache) / "public-knowledge.json"
    previous = None
    try:
        if path.is_file():
            previous = validate(json.loads(path.read_text()))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        raw, _ = fetch(SNAPSHOT_URL, MAX_BYTES, manifest=True)
        value = validate(json.loads(raw))
        if previous and instant(value["collected_at"]) < instant(
            previous["collected_at"]
        ):
            raise ValueError("拒绝用旧快照覆盖较新的缓存")
        atomic_json(path, value)
        state = (
            "current"
            if previous and previous["snapshot_sha256"] == value["snapshot_sha256"]
            else "updated"
        )
        return {"status": state, "snapshot_path": str(path), **summary(value)}
    except (OSError, ValueError, KeyError, TypeError):
        if previous:
            return {
                "status": "unavailable_using_cached",
                "snapshot_path": str(path),
                **summary(previous),
            }
        return {"status": "unavailable", "snapshot_path": None, "freshness": "unknown"}


def search(path, query, top=6):
    value = validate(json.loads(Path(path).read_text()))
    terms = set(tokenize(query))
    rows = []
    now = datetime.now(timezone.utc)
    for item in value["items"]:
        score = len(terms & set(tokenize(item["title"])))
        if not score:
            continue
        fresh = (
            bool(item.get("last_success_at"))
            and now - instant(item["last_success_at"]) <= MAX_AGE
        )
        rows.append(
            {
                **item,
                "score": score,
                "freshness": "fresh"
                if fresh and item["status"] == "ok"
                else "stale_or_failed",
            }
        )
    rows.sort(
        key=lambda x: (
            -x["score"],
            x.get("published_on") is None,
            -(int((x.get("published_on") or "0000-00-00").replace("-", ""))),
        )
    )
    return {
        "status": "ok" if rows else "no_match",
        "snapshot": summary(value),
        "hits": rows[:top],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("query")
    p.add_argument("--snapshot", required=True, type=Path)
    args = p.parse_args()
    print(json.dumps(search(args.snapshot, args.query), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
