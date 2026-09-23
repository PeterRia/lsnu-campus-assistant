#!/usr/bin/env python3
"""Dependency-free evidence retrieval. Ranking is not a policy-validity decision."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHINA = timezone(timedelta(hours=8))
ALIASES = {
    "四六级": "大学英语 四级 六级 CET",
    "ddl": "截止 报名",
    "挂科": "不及格 重修 补考",
    "奖助": "奖学金 助学金",
    "转系": "转专业",
    "老校区": "海棠",
    "新校区": "苏稽",
    "教资": "教师资格",
    "贫困认定": "家庭经济困难 认定",
    "成绩单": "学业成绩证明",
    "三笔字": "三字一话",
}


def today():
    return datetime.now(CHINA).date()


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_catalog(root=ROOT):
    catalog = json.loads((root / "kb/catalog.json").read_text(encoding="utf-8"))
    return catalog


def card_text(card, root=ROOT):
    path = (root / card["doc_path"]).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("doc_path must stay inside the skill")
    return path.read_text(encoding="utf-8")


def tokenize(value):
    value = value.lower()
    parts = re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+", value)
    tokens = []
    for part in parts:
        if re.fullmatch(r"[a-z0-9]+", part) or len(part) == 1:
            tokens.append(part)
        else:
            tokens.extend(part[i : i + 2] for i in range(len(part) - 1))
    return tokens


def temporal(card, when):
    start, end = card.get("effective_from"), card.get("effective_to")
    if start and when < date.fromisoformat(start):
        return "not_yet_applicable"
    if end and when > date.fromisoformat(end):
        return "outside_stated_period"
    if start or end:
        return "within_stated_period_recheck_updates"
    return "validity_not_established"


def search(query, when=None, top=5, category=None, root=ROOT):
    when = when or today()
    if not query.strip() or not tokenize(query):
        raise ValueError("请输入有效查询词")
    catalog = load_catalog(root)
    cards = [c for c in catalog["cards"] if not category or c["category"] == category]
    expanded = query
    for key, values in ALIASES.items():
        if key in query.lower():
            expanded += " " + values
    terms = set(tokenize(expanded))
    docs = [(c, card_text(c, root)) for c in cards]
    counters = [
        Counter(tokenize(c["title"] + " " + " ".join(c["keywords"]) + " " + text))
        for c, text in docs
    ]
    avg = sum(sum(c.values()) for c in counters) / max(1, len(counters))
    df = Counter(t for c in counters for t in c)
    results = []
    for (card, text), freq in zip(docs, counters):
        score = 0.0
        length = sum(freq.values())
        for term in terms:
            f = freq[term]
            if f:
                idf = math.log(1 + (len(docs) - df[term] + 0.5) / (df[term] + 0.5))
                score += (
                    idf * (f * 2.2) / (f + 1.2 * (0.25 + 0.75 * length / max(avg, 1)))
                )
        if score <= 0:
            continue
        # Exact topic matches help distinguish short Chinese concepts, without conflating rules.
        for keyword in card["keywords"]:
            if keyword.lower() in query.lower():
                score += 5
        if query in card["title"]:
            score += 8
        status = temporal(card, when)
        warnings = list(card.get("limitations", []))
        if status in {"outside_stated_period", "not_yet_applicable"}:
            warnings.append("目标日期不在该材料明确的适用时段内，不可直接作为当期安排")
        if (when - date.fromisoformat(card["verified_on"])).days > card.get(
            "recheck_after_days", 30
        ):
            warnings.append("超过建议复核间隔，需核对官方后续通知")
        results.append(
            {
                **card,
                "score": round(score, 3),
                "temporal_status": status,
                "warnings": warnings,
                "excerpt": text[:850],
            }
        )
    results.sort(key=lambda c: (-c["score"], c["id"]))
    return {
        "status": "ok" if results else "no_match",
        "query": query,
        "expanded_query": expanded,
        "as_of": when.isoformat(),
        "knowledge_verified_on": catalog["verified_on"],
        "hits": results[:top],
        "note": "检索命中不代表现行有效或个人符合条件；请回读完整证据卡和官方原文。",
    }


class PageText(HTMLParser):
    """Extract visible text; prefer the site's article container when available."""

    def __init__(self):
        super().__init__()
        self.skip = 0
        self.div_depth = 0
        self.article_depth = None
        self.all = []
        self.article = []
        self.titles = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag in {"script", "style", "noscript"}:
            self.skip += 1
        if tag == "title":
            self.in_title = True
        if tag == "div":
            self.div_depth += 1
            if "v_news_content" in attr.get("class", "").split():
                self.article_depth = self.div_depth

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.skip = max(0, self.skip - 1)
        if tag == "title":
            self.in_title = False
        if tag == "div":
            if self.article_depth == self.div_depth:
                self.article_depth = None
            self.div_depth = max(0, self.div_depth - 1)

    def handle_data(self, data):
        if self.skip:
            return
        data = data.strip()
        if not data:
            return
        self.all.append(data)
        if self.article_depth is not None:
            self.article.append(data)
        if self.in_title:
            self.titles.append(data)

    def result(self):
        text = "\n".join(self.article or self.all)
        return {
            "title": "".join(self.titles),
            "text": text,
            "content_sha256": digest(re.sub(r"\s+", "", text)),
            "extraction": "article" if self.article else "page",
        }


def official_url(url):
    p = urllib.parse.urlsplit(url)
    return (
        p.scheme in {"https", "http"}
        and not p.username
        and not p.password
        and (p.hostname == "lsnu.edu.cn" or (p.hostname or "").endswith(".lsnu.edu.cn"))
        and p.port in {None, 80, 443}
    )


class OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not official_url(newurl):
            raise ValueError("跳转离开乐师官方域名，停止自动读取")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_source(card, timeout=15):
    url = card["source_url"]
    if not official_url(url):
        return {"id": card["id"], "status": "unsupported_source", "url": url}
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "LSNU-Campus-Assistant/1.0 (public source verification)"
            },
        )
        with urllib.request.build_opener(OfficialRedirect()).open(
            req, timeout=timeout
        ) as r:
            content = r.read(2_000_001)
            if len(content) > 2_000_000:
                raise ValueError("页面超过 2 MB，请通过浏览器读取")
            if "html" not in r.headers.get("Content-Type", ""):
                return {
                    "id": card["id"],
                    "status": "attachment_requires_review",
                    "url": r.url,
                }
            charset = r.headers.get_content_charset() or "utf-8"
            html = content.decode(charset, errors="replace")
            final = r.url
        parser = PageText()
        parser.feed(html)
        parsed = parser.result()
        if any(
            x in parsed["text"]
            for x in ["请输入验证码下载附件", "人机验证", "Access Denied"]
        ):
            status = "access_challenge"
        elif not parsed["title"] or len(parsed["text"]) < 30:
            status = "unusable_response"
        elif card.get("content_sha256"):
            status = (
                "unchanged_snapshot"
                if card["content_sha256"] == parsed["content_sha256"]
                else "changed_requires_review"
            )
        else:
            status = "retrieved_requires_review"
        return {
            "id": card["id"],
            "status": status,
            "url": final,
            **parsed,
            "checked_at": datetime.now(CHINA).isoformat(),
            "note": "页面相同也不排除其他页面发布了替代通知；本命令不会更新知识卡。",
        }
    except (OSError, ValueError, LookupError, urllib.error.URLError) as e:
        return {"id": card["id"], "status": "fetch_error", "url": url, "error": str(e)}


def doctor(root=ROOT):
    catalog = load_catalog(root)
    errors = []
    seen = set()
    for c in catalog["cards"]:
        for key in [
            "id",
            "title",
            "category",
            "source_url",
            "publisher",
            "published_on",
            "verified_on",
            "effective_from",
            "effective_to",
            "audience",
            "doc_path",
            "keywords",
            "limitations",
        ]:
            if key not in c:
                errors.append(f"{c.get('id')}: missing {key}")
        if c["id"] in seen:
            errors.append("duplicate id: " + c["id"])
        seen.add(c["id"])
        if not official_url(c["source_url"]):
            errors.append("unverified domain: " + c["id"])
        for key in ["published_on", "verified_on", "effective_from", "effective_to"]:
            if c.get(key):
                date.fromisoformat(c[key])
        if (
            c.get("effective_from")
            and c.get("effective_to")
            and c["effective_from"] > c["effective_to"]
        ):
            errors.append("reversed validity: " + c["id"])
        text = card_text(c, root)
        if c["source_url"] not in text:
            errors.append("source missing from card: " + c["id"])
        if digest(text) != c.get("card_sha256"):
            errors.append("card changed without catalog refresh: " + c["id"])
    return {
        "status": "error" if errors else "ok",
        "cards": len(seen),
        "errors": errors,
        "python": sys.version.split()[0],
        "network_required": False,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("search")
    q.add_argument("query")
    q.add_argument("--as-of", type=date.fromisoformat, default=today())
    q.add_argument("--top", type=int, default=5)
    q.add_argument("--category")
    q.add_argument("--json", action="store_true", help="兼容选项；始终输出 JSON")
    s = sub.add_parser("show")
    s.add_argument("id")
    sub.add_parser("doctor")
    c = sub.add_parser("check-sources")
    c.add_argument("--id", required=True, help="仅复核指定证据卡，避免无目的全站抓取")
    args = p.parse_args()
    try:
        if args.cmd == "search":
            if not 1 <= args.top <= 20:
                raise ValueError("--top must be 1..20")
            out = search(args.query, args.as_of, args.top, args.category)
        elif args.cmd == "doctor":
            out = doctor()
        else:
            card = next(
                (c for c in load_catalog()["cards"] if c["id"] == args.id), None
            )
            if card is None:
                raise ValueError("未知证据 ID")
            if args.cmd == "show":
                print(card_text(card))
                return
            out = check_source(card)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        if out.get("status") in {
            "error",
            "fetch_error",
            "access_challenge",
            "unusable_response",
        }:
            sys.exit(2)
    except (OSError, ValueError, KeyError) as e:
        print(
            json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False),
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
