"""Behavioral checks for evidence, planning and summaries; no network or accounts."""

import copy
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import campus
import credits
import planner
import progress

ROOT = Path(__file__).resolve().parents[1]


def event(id, start, end, campus="海棠", location="A"):
    return {
        "id": id,
        "title": id,
        "start": "2026-09-25T" + start + "+08:00",
        "end": "2026-09-25T" + end + "+08:00",
        "campus": campus,
        "location": location,
    }


class EvidenceTests(unittest.TestCase):
    def test_integrity(self):
        self.assertEqual(campus.doctor()["status"], "ok")

    def test_retrieval_benchmarks(self):
        cases = {
            "重修报名缴费": "retake-2026-autumn",
            "四六级报名": "cet-2026-autumn",
            "困难认定": "hardship-2026",
            "校长奖学金": "president-award-2026",
            "苏稽图书馆开放": "suji-library-opening",
            "转专业": "transfer-overview",
            "校历周次": "calendar-2026-2027",
            "成绩证明": "transcript-guide",
            "校外文献 VPN": "library-off-campus",
            "海棠苏稽地址": "campuses",
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                hits = campus.search(query, date(2026, 9, 23), top=3)["hits"]
                self.assertIn(expected, [h["id"] for h in hits])
                self.assertTrue(all(h["source_url"] and h["doc_path"] for h in hits))

    def test_historical_window_not_current(self):
        hit = next(
            h
            for h in campus.search("鼎堂", date(2026, 10, 1), top=20)["hits"]
            if h["id"] == "suji-library-opening"
        )
        self.assertEqual(hit["temporal_status"], "outside_stated_period")

    def test_publication_not_effective_date(self):
        hit = campus.search("缓考", date(2026, 9, 23))["hits"][0]
        self.assertEqual(hit["temporal_status"], "validity_not_established")

    def test_empty_not_technical_error(self):
        self.assertEqual(campus.search("zzxqvnomatch")["status"], "no_match")
        with self.assertRaises(ValueError):
            campus.search(" ")

    def test_public_host_boundary(self):
        for url in [
            "https://lsnu.edu.cn.evil.example/",
            "https://user:pw@www.lsnu.edu.cn/",
            "file:///tmp/foo",
            "http://127.0.0.1/",
        ]:
            self.assertFalse(campus.official_url(url))
        self.assertTrue(
            campus.official_url("https://jiaowc.lsnu.edu.cn/info/1017/12183.htm")
        )

    def test_article_fingerprint_ignores_counter_script(self):
        p = campus.PageText()
        p.feed(
            '<title>A</title><div>导航</div><div class="v_news_content"><p>正文</p><script>counter(42)</script><div>条款</div></div>页脚'
        )
        self.assertEqual(p.result()["text"], "正文\n条款")


class PlanningTests(unittest.TestCase):
    def test_nested_overlaps(self):
        r = planner.plan(
            {
                "events": [
                    event("long", "09:00", "12:00"),
                    event("a", "09:30", "10:00"),
                    event("b", "11:00", "11:30"),
                ]
            }
        )
        self.assertEqual(
            len([c for c in r["conflicts"] if c["type"] == "time_overlap"]), 2
        )

    def test_deadline_does_not_occupy_route(self):
        r = planner.plan(
            {
                "events": [event("a", "09:00", "10:00"), event("b", "10:00", "11:00")],
                "deadlines": [
                    {"id": "d", "title": "线上截止", "due": "2026-09-25T10:00+08:00"}
                ],
            }
        )
        self.assertFalse(r["conflicts"])
        self.assertEqual([(t["from"], t["to"]) for t in r["transitions"]], [("a", "b")])

    def test_cross_campus_unknown_not_zero(self):
        r = planner.plan(
            {
                "events": [
                    event("a", "09:00", "10:00"),
                    event("b", "10:30", "11:00", "苏稽", "A"),
                ]
            }
        )
        self.assertEqual(r["planning_status"], "travel_unverified")
        self.assertIsNone(r["transitions"][0]["travel_minutes"])

    def test_known_route_buffer(self):
        r = planner.plan(
            {
                "events": [
                    event("a", "09:00", "10:00"),
                    event("b", "10:30", "11:00", "苏稽", "B"),
                ],
                "routes": [
                    {
                        "from": "A",
                        "to": "B",
                        "from_campus": "海棠",
                        "to_campus": "苏稽",
                        "minutes": 25,
                        "source": "用户提供的测试估计",
                    }
                ],
                "arrival_buffer_minutes": 10,
            }
        )
        self.assertEqual(r["conflicts"][0]["type"], "travel_buffer")

    def test_reverse_route_not_assumed(self):
        r = planner.plan(
            {
                "events": [
                    event("a", "09:00", "10:00", "苏稽", "B"),
                    event("b", "11:00", "12:00"),
                ],
                "routes": [
                    {
                        "from": "A",
                        "to": "B",
                        "from_campus": "海棠",
                        "to_campus": "苏稽",
                        "minutes": 25,
                        "source": "示例",
                    }
                ],
            }
        )
        self.assertEqual(r["planning_status"], "travel_unverified")

    def test_nonfinite_route_rejected(self):
        d = {
            "events": [
                event("a", "09:00", "10:00"),
                event("b", "11:00", "12:00", "苏稽", "B"),
            ],
            "routes": [
                {
                    "from": "A",
                    "to": "B",
                    "from_campus": "海棠",
                    "to_campus": "苏稽",
                    "minutes": float("nan"),
                    "source": "bad",
                }
            ],
        }
        with self.assertRaises(ValueError):
            planner.plan(d)

    def test_single_event_missing_location_flagged(self):
        e = event("a", "09:00", "10:00")
        del e["location"]
        r = planner.plan({"events": [e]})
        self.assertEqual(r["planning_status"], "travel_unverified")
        self.assertEqual(r["incomplete_location_ids"], ["a"])

    def test_invalid_interval_or_duplicate_fails(self):
        with self.assertRaises(ValueError):
            planner.plan({"events": [event("a", "10:00", "09:00")]})
        with self.assertRaises(ValueError):
            planner.plan({"events": [event("a", "09:00", "10:00")] * 2})

    def test_date_only_deadline_ics(self):
        r = planner.plan(
            {
                "deadlines": [
                    {
                        "id": "d",
                        "title": "日前申请",
                        "due": "2026-10-08",
                        "precision": "date",
                    }
                ]
            }
        )
        ics = planner.calendar(r)
        self.assertIn("BEGIN:VTODO", ics)
        self.assertIn("DUE;VALUE=DATE:20261008", ics)
        self.assertNotIn("BEGIN:VEVENT", ics)
        self.assertNotIn("VALARM", ics)

    def test_ics_timezone_escape_and_utf8_folding(self):
        e = event("a", "09:00", "10:00")
        e["title"] = "测试很长的中文标题" * 20 + "\nLOCATION:fake"
        e["alarm_minutes"] = 15
        r = planner.plan({"events": [e]})
        ics = planner.calendar(r)
        self.assertIn("DTSTART:20260925T010000Z", ics)
        self.assertNotIn("\r\nLOCATION:fake", ics)
        self.assertIn("\\nLOCATION:fake", ics.replace("\r\n ", ""))
        self.assertTrue(all(len(line.encode()) <= 75 for line in ics.split("\r\n")))

    def test_week_anchor_and_exceptions(self):
        d = json.loads((ROOT / "examples/plan-demo.json").read_text())
        r = planner.plan(d)
        courses = [e for e in r["events"] if e["id"].startswith("demo-course")]
        self.assertEqual(len(courses), 1)
        self.assertTrue(courses[0]["start"].startswith("2026-09-21"))

    def test_flexible_no_slot(self):
        f = {
            "id": "f",
            "title": "办事",
            "duration_minutes": 30,
            "campus": "海棠",
            "location": "A",
            "windows": [{"start": "2026-09-25T09:00", "end": "2026-09-25T10:00"}],
        }
        r = planner.plan({"events": [event("a", "09:00", "10:00")], "flexible": [f]})
        self.assertEqual(r["flexible_proposals"][0]["status"], "no_feasible_slot")

    def test_flexible_respects_travel(self):
        f = {
            "id": "f",
            "title": "办事",
            "duration_minutes": 20,
            "campus": "海棠",
            "location": "B",
            "windows": [{"start": "2026-09-25T10:00", "end": "2026-09-25T11:00"}],
        }
        route = {
            "from": "A",
            "to": "B",
            "from_campus": "海棠",
            "to_campus": "海棠",
            "minutes": 15,
            "source": "测试输入",
        }
        r = planner.plan(
            {
                "events": [event("a", "09:00", "10:00")],
                "flexible": [f],
                "routes": [route],
                "arrival_buffer_minutes": 5,
            }
        )
        self.assertTrue(
            r["flexible_proposals"][0]["alternatives"][0]["start"].startswith(
                "2026-09-25T10:20"
            )
        )

    def test_flexible_missing_location_is_unverified(self):
        task = {
            "id": "f",
            "title": "办事",
            "duration_minutes": 20,
            "windows": [{"start": "2026-09-25T10:00", "end": "2026-09-25T11:00"}],
        }
        result = planner.plan({"flexible": [task]})
        self.assertEqual(
            result["flexible_proposals"][0]["alternatives"][0]["status"],
            "time_fits_travel_unverified",
        )

    def test_private_output_outside_package(self):
        with self.assertRaises(ValueError):
            planner.save_external(ROOT / "private-output.json", "{}")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "a.json"
            planner.save_external(path, "{}")
            with self.assertRaises(ValueError):
                planner.save_external(path, "{}")


class ProgressTests(unittest.TestCase):
    def test_bookings_not_completed(self):
        d = json.loads((ROOT / "examples/progress-demo.json").read_text())
        r = progress.summarize(d, date(2026, 9, 14), date(2026, 9, 20))
        self.assertEqual(
            (r["completed_sessions"], r["confirmed_minutes"], r["attendance_only"]),
            (3, 135, 1),
        )

    def test_dedup_and_conflicting_record(self):
        d = json.loads((ROOT / "examples/progress-demo.json").read_text())
        d["records"].append(copy.deepcopy(d["records"][0]))
        self.assertEqual(
            progress.summarize(d, date(2026, 9, 14), date(2026, 9, 20))[
                "completed_sessions"
            ],
            3,
        )
        d["records"][-1]["minutes"] = 99
        with self.assertRaises(ValueError):
            progress.summarize(d, date(2026, 9, 14), date(2026, 9, 20))

    def test_missing_duration_not_invented(self):
        d = {
            "records": [
                {
                    "id": "x",
                    "activity": "步行",
                    "date": "2026-09-14",
                    "status": "completed",
                    "evidence": "user_confirmed",
                }
            ]
        }
        r = progress.summarize(d, date(2026, 9, 14), date(2026, 9, 20))
        self.assertEqual(r["confirmed_minutes"], 0)
        self.assertEqual(r["activities"]["步行"]["duration_missing"], 1)


class CreditTests(unittest.TestCase):
    def test_duplicates_and_unknown_category(self):
        d = json.loads((ROOT / "examples/credits-demo.json").read_text())
        r = credits.audit(d)
        self.assertEqual(r["categories"][0]["recognized_earned"], 3)
        self.assertEqual(r["categories"][0]["remaining"], 7)
        self.assertEqual(len(r["unknown"]), 1)

    def test_not_passed_not_counted(self):
        r = credits.audit(
            {
                "requirements": [{"category": "A", "credits": 2}],
                "courses": [
                    {
                        "course_code": "x",
                        "category": "A",
                        "credits": 2,
                        "passed": False,
                        "recognized": True,
                        "recognition_source": "test",
                    }
                ],
            }
        )
        self.assertEqual(r["categories"][0]["remaining"], 2)

    def test_conflicting_recognition_rejected(self):
        d = json.loads((ROOT / "examples/credits-demo.json").read_text())
        d["courses"][1]["credits"] = 4
        with self.assertRaises(ValueError):
            credits.audit(d)


if __name__ == "__main__":
    unittest.main()
