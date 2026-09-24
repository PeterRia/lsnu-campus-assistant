"""Bounded official-notice discovery and fingerprint collection for GitHub Actions.

Publishes titles, dates, URLs and source status, never full pages or local user data.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from campus import ROOT, OfficialRedirect, PageText, official_url
from public_knowledge import SKILL, checksum, seal, validate
from public_update import atomic_json


class Metadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.href = None
        self.words = []
        self.date = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.href, self.words = attrs.get("href"), []
        if tag == "meta" and (
            attrs.get("name") or attrs.get("property") or ""
        ).lower() in {"pubdate", "publishdate", "article:published_time", "date"}:
            match = re.search(r"\d{4}-\d{2}-\d{2}", attrs.get("content", ""))
            if match:
                self.date = match[0]

    def handle_data(self, data):
        if self.href:
            self.words.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            self.links.append((self.href, " ".join(self.words).strip()))
            self.href = None


def fetch_page(url):
    if not official_url(url):
        raise ValueError("只允许明确的乐师官方来源")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), OfficialRedirect()
    )
    request = urllib.request.Request(
        url, headers={"User-Agent": "lsnu-compus-skill/1.2 public-notice-monitor"}
    )
    with opener.open(request, timeout=12) as response:
        content = response.read(2_000_001)
        if len(content) > 2_000_000 or "html" not in response.headers.get(
            "Content-Type", ""
        ):
            raise ValueError("附件或过大页面需要单独核查")
        html = content.decode(
            response.headers.get_content_charset() or "utf-8", errors="replace"
        )
    return parse_page(html, url)


def parse_page(html, url):
    page, metadata = PageText(), Metadata()
    page.feed(html)
    metadata.feed(html)
    parsed = page.result()
    if (
        not parsed["title"]
        or len(parsed["text"]) < 30
        or any(
            x in parsed["text"]
            for x in ("请输入验证码下载附件", "人机验证", "Access Denied")
        )
    ):
        raise ValueError("未取得可用官方页面")
    published = metadata.date
    if not published:
        header = " ".join(page.all)
        if page.article:
            header = header.split(page.article[0], 1)[0]
        match = re.search(
            r"(?:发布时间|发布日期|发布于|(?<!\S)日期)[：:\s]*(\d{4})[-年/](\d{1,2})[-月/](\d{1,2})",
            header,
        )
        if match:
            published = "-".join([match[1], match[2].zfill(2), match[3].zfill(2)])
    if published:
        datetime.strptime(published, "%Y-%m-%d")
    return {
        "title": parsed["title"][:300],
        "published_on": published,
        "content_sha256": parsed["content_sha256"],
        "links": [
            (urllib.parse.urljoin(url, href), text) for href, text in metadata.links
        ],
    }


def collect(cards, feeds, previous=None, fetch=fetch_page, collected_at=None):
    at = collected_at or datetime.now(timezone.utc).isoformat()
    previous = validate(previous) if previous else {"items": [], "feeds": []}
    old_items = {x["url"]: x for x in previous["items"]}
    old_feeds = {x["url"]: x for x in previous["feeds"]}
    if len(feeds) > 6 or any(
        not official_url(f["url"]) or not 1 <= f["limit"] <= 12 for f in feeds
    ):
        raise ValueError("通知入口范围无效")
    targets = {}
    for card in cards:
        url = card["source_url"]
        if not official_url(url):
            raise ValueError("知识卡来源越界")
        row = targets.setdefault(
            url, {"title": card["title"], "tracked_card_ids": [], "discovered_from": []}
        )
        row["tracked_card_ids"].append(card["id"])

    def attempt(url):
        try:
            return fetch(url), None
        except (OSError, ValueError, LookupError) as exc:
            return None, type(exc).__name__

    observations = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(attempt, [f["url"] for f in feeds]))
    for feed, (page, error) in zip(feeds, results):
        if not official_url(feed["url"]):
            raise ValueError("通知入口越界")
        found = []
        if page:
            for url, title in page["links"]:
                part = urllib.parse.urlsplit(url)
                section = re.fullmatch(r"/info/(\d+)/\d+\.htm", part.path)
                if (
                    official_url(url)
                    and part.netloc == urllib.parse.urlsplit(feed["url"]).netloc
                    and section
                    and section[1] in feed["sections"]
                    and not part.query
                    and title
                    and url not in [x[0] for x in found]
                ):
                    found.append((url, title[:300]))
                if len(found) >= feed["limit"]:
                    break
            if not found:
                error = "NoUsableNoticeLinks"
        old_feed = old_feeds.get(feed["url"], {})
        if error:
            found = [(x["url"], x["title"]) for x in old_feed.get("notices", [])]
        for url, title in found:
            row = targets.setdefault(
                url, {"title": title, "tracked_card_ids": [], "discovered_from": []}
            )
            row["discovered_from"].append(feed["name"])
        observations.append(
            {
                "url": feed["url"],
                "name": feed["name"],
                "status": "error" if error else "ok",
                "error": error,
                "checked_at": at,
                "last_success_at": old_feed.get("last_success_at") if error else at,
                "notices": [{"url": url, "title": title} for url, title in found],
            }
        )
    if len(targets) > 100:
        raise ValueError("单次采集范围超过限制")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, targets))
    records = dict(old_items)
    changed, new = 0, 0
    for (url, target), (page, error) in zip(targets.items(), results):
        old = old_items.get(url, {})
        row = {
            **old,
            **target,
            "id": checksum(url)[:16],
            "url": url,
            "checked_at": at,
            "status": "error" if error else "ok",
            "error": error,
            "review_status": "automatic_observation",
        }
        if page:
            different = any(
                old.get(k) != page[k]
                for k in ("title", "published_on", "content_sha256")
            )
            change = (
                "new"
                if not old.get("content_sha256")
                else ("changed" if different else "unchanged")
            )
            changed += change == "changed"
            new += change == "new"
            row.update(
                {k: page[k] for k in ("title", "published_on", "content_sha256")},
                last_success_at=at,
                change=change,
            )
        else:
            # An attempted fetch must not refresh a failed source's success time.
            row.update(
                last_success_at=old.get("last_success_at"), change="not_verified"
            )
            row.setdefault("published_on", None)
            row.setdefault("content_sha256", None)
        records[url] = row
    items = sorted(
        records.values(),
        key=lambda x: (x["url"] in targets, x.get("last_success_at") or ""),
        reverse=True,
    )[:250]
    items.sort(key=lambda x: x["url"])
    content_revision = checksum(
        [
            {k: x.get(k) for k in ("url", "title", "published_on", "content_sha256")}
            for x in items
        ]
    )
    value = seal(
        {
            "schema_version": 1,
            "skill_id": SKILL,
            "collected_at": at,
            "content_revision": content_revision,
            "feeds": observations,
            "items": items,
            "stats": {
                "attempted": len(targets),
                "succeeded": sum(x[0] is not None for x in results),
                "failed": sum(x[0] is None for x in results),
                "new": new,
                "changed": changed,
            },
            "scope": "自动观察公开通知标题、发布日期、来源与正文指纹，不复制全文、不自动改写已审核知识卡，不代表政策有效性或人工核查。",
        }
    )
    return validate(value)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--previous", type=Path)
    p.add_argument("--report", type=Path)
    args = p.parse_args()
    previous = (
        json.loads(args.previous.read_text())
        if args.previous and args.previous.is_file()
        else None
    )
    cards = json.loads((ROOT / "kb/catalog.json").read_text())["cards"]
    feeds = json.loads((ROOT / "kb/public-sources.json").read_text())["feeds"]
    value = collect(cards, feeds, previous)
    atomic_json(args.output, value)
    report = {
        "collected_at": value["collected_at"],
        "content_revision": value["content_revision"],
        **value["stats"],
        "failed_feeds": sum(f["status"] != "ok" for f in value["feeds"]),
    }
    if args.report:
        atomic_json(args.report, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
