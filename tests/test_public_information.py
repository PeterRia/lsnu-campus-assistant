"""Behavior checks for scheduled public discovery and independent local synchronization."""

# ruff: noqa: E402 -- installed folder Skill, no package installation.

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import collect_public as collector
import public_knowledge as knowledge
import public_update
from campus import OfficialRedirect

FEED = "https://libnew.lsnu.edu.cn/"
NOTICE = FEED + "info/1004/4061.htm"
FIRST = "2026-09-20T01:00:00+00:00"
SECOND = "2026-09-20T02:00:00+00:00"


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.feeds = [{"name": "图书馆", "url": FEED, "sections": ["1004"], "limit": 8}]
        self.pages = {
            FEED: {"links": [(NOTICE, "图书馆中秋国庆开馆通知")]},
            NOTICE: {
                "title": "图书馆中秋国庆开馆通知",
                "published_on": "2026-09-19",
                "content_sha256": "a" * 64,
                "links": [],
            },
        }

    def snapshot(self, previous=None, at=FIRST, fetch=None):
        return collector.collect(
            [], self.feeds, previous, fetch or self.pages.__getitem__, at
        )

    def test_new_notice_discovered_without_a_reviewed_card(self):
        result = self.snapshot()
        self.assertEqual(
            result["stats"],
            {"attempted": 1, "succeeded": 1, "failed": 0, "new": 1, "changed": 0},
        )
        self.assertEqual(result["items"][0]["url"], NOTICE)
        self.assertEqual(result["items"][0]["review_status"], "automatic_observation")
        self.assertEqual(result["items"][0]["tracked_card_ids"], [])
        self.assertNotIn("text", result["items"][0])

    def test_poll_time_changes_without_a_false_content_change(self):
        first = self.snapshot()
        second = self.snapshot(first, SECOND)
        self.assertEqual(first["content_revision"], second["content_revision"])
        self.assertNotEqual(first["snapshot_sha256"], second["snapshot_sha256"])
        self.assertEqual(second["items"][0]["change"], "unchanged")
        self.pages[NOTICE]["content_sha256"] = "b" * 64
        changed = self.snapshot(second, SECOND)
        self.assertNotEqual(second["content_revision"], changed["content_revision"])
        self.assertEqual(changed["stats"]["changed"], 1)
        self.pages[NOTICE]["published_on"] = "2026-09-20"
        self.assertEqual(self.snapshot(changed, SECOND)["stats"]["changed"], 1)

    def test_failed_refresh_preserves_last_success_and_content(self):
        first = self.snapshot()

        def offline(url):
            raise OSError("synthetic network failure")

        failed = self.snapshot(first, SECOND, offline)
        self.assertEqual(failed["items"][0]["last_success_at"], FIRST)
        self.assertEqual(failed["items"][0]["content_sha256"], "a" * 64)
        self.assertEqual(failed["feeds"][0]["last_success_at"], FIRST)
        self.assertEqual(
            knowledge.summary(failed, datetime.fromisoformat(SECOND))["freshness"],
            "partial",
        )
        self.assertEqual(
            knowledge.summary(
                failed, datetime.fromisoformat("2026-09-20T06:00:00+00:00")
            )["freshness"],
            "stale",
        )

    def test_empty_feed_is_failure_and_retains_previous_discovery(self):
        first = self.snapshot()
        self.pages[FEED]["links"] = []
        result = self.snapshot(first, SECOND)
        self.assertEqual(result["feeds"][0]["error"], "NoUsableNoticeLinks")
        self.assertEqual(result["feeds"][0]["last_success_at"], FIRST)
        self.assertEqual(result["items"][0]["url"], NOTICE)

    def test_source_scope_and_redirect_are_enforced(self):
        self.pages[FEED]["links"] += [
            ("https://other.example/info/1004/1.htm", "outside"),
            (FEED + "info/9999/1.htm", "other section"),
        ]
        self.assertEqual(len(self.snapshot()["items"]), 1)
        self.feeds[0]["url"] = "https://other.example/"
        with self.assertRaises(ValueError):
            self.snapshot()
        with self.assertRaises(ValueError):
            OfficialRedirect().redirect_request(
                None, None, 302, "", {}, "https://other.example/"
            )

    def test_explicit_publication_date_is_distinct_from_deadline(self):
        html = '<title>图书馆通知</title><meta name="PubDate" content="2026-09-19"><div class="v_news_content">本通知安排假期开放服务，申请截止2026年10月7日，详细时间请核对官方通知。</div>'
        self.assertEqual(
            collector.parse_page(html, NOTICE)["published_on"], "2026-09-19"
        )
        without = html.replace('<meta name="PubDate" content="2026-09-19">', "")
        self.assertIsNone(collector.parse_page(without, NOTICE)["published_on"])
        header = without.replace(
            '<div class="v_news_content">',
            '<p>日期：2026-09-19 作者：图书馆</p><div class="v_news_content">',
        )
        self.assertEqual(
            collector.parse_page(header, NOTICE)["published_on"], "2026-09-19"
        )
        with self.assertRaises(ValueError):
            collector.parse_page(
                "<title>验证</title><p>请输入验证码下载附件，请输入验证码以后才能下载官方文件，否则不能继续完成此次操作。</p>",
                NOTICE,
            )

    def test_tamper_and_impossible_status_rejected(self):
        value = self.snapshot()
        value["items"][0]["title"] = "tampered"
        with self.assertRaises(ValueError):
            knowledge.validate(value)
        value = self.snapshot()
        value["items"][0]["last_success_at"] = SECOND
        with self.assertRaises(ValueError):
            knowledge.validate(knowledge.seal(value))
        value = self.snapshot()
        value["feeds"][0]["notices"][0]["url"] = "https://other.example/"
        with self.assertRaises(ValueError):
            knowledge.validate(knowledge.seal(value))

    def test_corrupt_or_older_remote_keeps_verified_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            newest = self.snapshot(at=SECOND)
            public_update.atomic_json(Path(tmp) / "public-knowledge.json", newest)
            for raw in (b'{"invalid": true}', json.dumps(self.snapshot()).encode()):
                result = knowledge.sync(tmp, lambda *a, **kw: (raw, None))
                self.assertEqual(result["status"], "unavailable_using_cached")
                self.assertEqual(result["collected_at"], SECOND)
                self.assertEqual(result["freshness"], "stale")
                self.assertEqual(
                    json.loads((Path(tmp) / "public-knowledge.json").read_text()),
                    newest,
                )

    def test_search_returns_new_notice_with_its_own_freshness(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            public_update.atomic_json(path, self.snapshot())
            result = knowledge.search(path, "图书馆 中秋 国庆")
            self.assertEqual(result["hits"][0]["url"], NOTICE)
            self.assertEqual(result["hits"][0]["freshness"], "stale_or_failed")

    def test_code_unchanged_still_downloads_new_public_information(self):
        manifest = {
            "schema_version": 1,
            "skill_id": knowledge.SKILL,
            "version": (ROOT / "VERSION").read_text().strip(),
            "python_min": "3.10",
            "permissions": public_update.PERMISSIONS,
            "archive_url": "https://github.com/PeterRia/lsnu-compus-skill/releases/download/v1.2.0/lsnu-compus-skill-1.2.0.zip",
            "archive_sha256": "a" * 64,
        }
        snapshot = self.snapshot()
        requests = []

        def fetch(url, limit, **kwargs):
            requests.append(url)
            return json.dumps(
                manifest if url == public_update.MANIFEST_URL else snapshot
            ).encode(), None

        with tempfile.TemporaryDirectory() as tmp:
            first = public_update.update({"installed": str(ROOT), "cache": tmp}, fetch)
            snapshot = self.snapshot(snapshot, SECOND)
            second = public_update.update({"installed": str(ROOT), "cache": tmp}, fetch)
        self.assertEqual(first["update_status"], "current")
        self.assertEqual(second["update_status"], "current")
        self.assertEqual(second["knowledge"]["status"], "updated")
        self.assertEqual(second["knowledge"]["collected_at"], SECOND)
        self.assertEqual(
            requests, [public_update.MANIFEST_URL, knowledge.SNAPSHOT_URL] * 2
        )

    def test_failure_does_not_invent_a_cached_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = knowledge.sync(tmp, lambda *a, **kw: (b"{}", None))
            self.assertEqual(result["status"], "unavailable")
            self.assertIsNone(result["snapshot_path"])
            self.assertFalse((Path(tmp) / "public-knowledge.json").exists())


if __name__ == "__main__":
    unittest.main()
