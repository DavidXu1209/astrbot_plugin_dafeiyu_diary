import sys, tempfile, unittest
from datetime import date, datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diary_core import CST, DiaryStore, build_prompt, parse_day_arg, seconds_until


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DiaryStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_and_transcript(self):
        t = datetime(2026, 11, 6, 14, 5, tzinfo=CST)
        self.store.append("owner", "今天好累", t)
        self.store.append("bot", "累就歇会儿", t)
        self.store.append("owner", "   ", t)
        tr = self.store.transcript("2026-11-06", 6000)
        self.assertEqual(tr, "[14:05] 他：今天好累\n[14:05] 我：累就歇会儿")

    def test_transcript_truncates_keeping_latest(self):
        t = datetime(2026, 11, 6, 9, 0, tzinfo=CST)
        for i in range(50):
            self.store.append("owner", f"消息{i:02d}" * 5, t)
        tr = self.store.transcript("2026-11-06", 100)
        self.assertLessEqual(len(tr), 100)
        self.assertIn("消息49", tr)

    def test_day_key_uses_beijing_time(self):
        utc_late = datetime(2026, 11, 6, 17, 0, tzinfo=CST.__class__(CST.utcoffset(None) * 0))
        self.store.append("owner", "x", utc_late)  # 17:00 UTC = 01:00 next day CST
        self.assertTrue((Path(self.tmp.name) / "logs" / "2026-11-07.jsonl").is_file())

    def test_diary_roundtrip_and_list(self):
        self.store.save_diary("2026-11-05", "a")
        self.store.save_diary("2026-11-06", "b")
        self.assertEqual(self.store.read_diary("2026-11-06"), "b\n")
        self.assertIsNone(self.store.read_diary("2026-11-01"))
        self.assertEqual(self.store.list_days(), ["2026-11-06", "2026-11-05"])

    def test_prune_logs_keeps_recent_and_diaries(self):
        for d in ["2026-10-20", "2026-11-01", "2026-11-05"]:
            (Path(self.tmp.name) / "logs" / f"{d}.jsonl").write_text("{}\n", encoding="utf-8")
        self.store.save_diary("2026-10-20", "old diary")
        removed = self.store.prune_logs(7, date(2026, 11, 6))
        self.assertEqual(removed, 1)
        self.assertTrue((Path(self.tmp.name) / "logs" / "2026-11-01.jsonl").exists())
        self.assertFalse((Path(self.tmp.name) / "logs" / "2026-10-20.jsonl").exists())
        self.assertTrue((Path(self.tmp.name) / "logs" / "2026-11-05.jsonl").exists())
        self.assertIsNotNone(self.store.read_diary("2026-10-20"))

    def test_parse_day_arg(self):
        today = date(2026, 11, 6)
        self.assertEqual(parse_day_arg("", today), "2026-11-06")
        self.assertEqual(parse_day_arg("昨天", today), "2026-11-05")
        self.assertEqual(parse_day_arg("11-3", today), "2026-11-03")
        self.assertEqual(parse_day_arg("2025-12-31", today), "2025-12-31")
        self.assertIsNone(parse_day_arg("13-40", today))
        self.assertIsNone(parse_day_arg("随便", today))

    def test_seconds_until(self):
        now = datetime(2026, 11, 6, 23, 0, tzinfo=CST)
        self.assertEqual(seconds_until("23:50", now), 50 * 60)
        self.assertEqual(seconds_until("22:00", now), 23 * 3600)
        self.assertEqual(seconds_until("bad", now), 50 * 60)

    def test_prompt_contains_rules_and_transcript(self):
        p = build_prompt("[10:00] 他：hi")
        self.assertIn("不编造", p)
        self.assertTrue(p.endswith("[10:00] 他：hi"))


if __name__ == "__main__":
    unittest.main()
