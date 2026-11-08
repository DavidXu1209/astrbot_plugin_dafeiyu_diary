import asyncio, importlib.util, sys, tempfile, types, unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def load(tmpdir):
    deco = lambda *a, **k: (lambda f: f)
    flt = SimpleNamespace(event_message_type=deco, after_message_sent=deco, command=deco, on_llm_request=deco,
                          EventMessageType=SimpleNamespace(PRIVATE_MESSAGE=1))
    mods = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": types.ModuleType("astrbot.api"),
        "astrbot.api.event": types.ModuleType("astrbot.api.event"),
        "astrbot.api.star": types.ModuleType("astrbot.api.star"),
    }
    mods["astrbot.api"].logger = SimpleNamespace(info=lambda *a: None, warning=lambda *a: None)
    mods["astrbot.api.event"].AstrMessageEvent = object
    mods["astrbot.api.event"].filter = flt
    class Star:
        def __init__(self, context): self.context = context
    mods["astrbot.api.star"].Star = Star
    mods["astrbot.api.star"].Context = object
    mods["astrbot.api.star"].StarTools = SimpleNamespace(get_data_dir=lambda name: tmpdir)
    sys.modules.update(mods)
    pkg = types.ModuleType("diarypkg"); pkg.__path__ = [str(ROOT)]; sys.modules["diarypkg"] = pkg
    spec = importlib.util.spec_from_file_location("diarypkg.main", ROOT / "main.py")
    m = importlib.util.module_from_spec(spec); sys.modules["diarypkg.main"] = m
    spec.loader.exec_module(m)
    return m


class Ev:
    def __init__(self, umo, text="", result=None):
        self.unified_msg_origin = umo; self.message_str = text; self._result = result; self.stopped = False
    def get_result(self): return self._result
    def plain_result(self, t): return SimpleNamespace(text=t, chain=[SimpleNamespace(text=t)])
    def stop_event(self): self.stopped = True


class Prov:
    def __init__(self, text=None, exc=None): self.text, self.exc, self.calls = text, exc, 0
    async def text_chat(self, **kw):
        self.calls += 1
        if self.exc: raise self.exc
        return SimpleNamespace(completion_text=self.text)


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load(self.tmp.name)
        self.provs = {"a": Prov(exc=RuntimeError("429")), "b": Prov(text="今天他又喊累。")}
        ctx = SimpleNamespace(get_provider_by_id=lambda pid: self.provs.get(pid))
        self.p = self.m.DiaryPlugin(ctx, {"owner_qq": "111", "adapter_id": "qq", "provider_ids": ["missing", "a", "b"]})
        self.owner = "qq:FriendMessage:111"

    def tearDown(self): self.tmp.cleanup()

    def run_(self, c): return asyncio.run(c)

    def test_only_owner_private_recorded_and_commands_skipped(self):
        self.run_(self.p.record_owner(Ev(self.owner, "今天好累")))
        self.run_(self.p.record_owner(Ev(self.owner, "/reset")))
        self.run_(self.p.record_owner(Ev(self.owner, "日记 昨天")))
        self.run_(self.p.record_owner(Ev("qq:FriendMessage:222", "陌生人")))
        self.run_(self.p.record_owner(Ev("qq:GroupMessage:9", "群消息")))
        day = self.m.day_key(self.m.now_cst())
        self.assertEqual([r["text"] for r in self.p.store.read_log(day)], ["今天好累"])

    def test_bot_reply_recorded_but_not_diary_or_reset_ack(self):
        r = SimpleNamespace(chain=[SimpleNamespace(text="歇会儿")])
        self.run_(self.p.record_reply(Ev(self.owner, result=r)))
        self.run_(self.p.record_reply(Ev(self.owner, result=SimpleNamespace(chain=[SimpleNamespace(text="✅ cleared")]))))
        d = SimpleNamespace(chain=[SimpleNamespace(text="日记内容")], _diary_skip=True)
        self.run_(self.p.record_reply(Ev(self.owner, result=d)))
        day = self.m.day_key(self.m.now_cst())
        self.assertEqual([r["role"] for r in self.p.store.read_log(day)], ["bot"])

    def test_write_falls_back_and_caches(self):
        self.run_(self.p.record_owner(Ev(self.owner, "今天好累")))
        day = self.m.day_key(self.m.now_cst())
        text = self.run_(self.p._write(day, update_notes=False))
        self.assertEqual(text, "今天他又喊累。")
        self.assertEqual((self.provs["a"].calls, self.provs["b"].calls), (1, 1))
        self.run_(self.p._write(day))
        self.assertEqual(self.provs["b"].calls, 1)

    def test_provisional_stays_until_finalize(self):
        self.run_(self.p.record_owner(Ev(self.owner, "下午甩了表情包")))
        day = self.m.day_key(self.m.now_cst())
        self.provs["b"].text = "临时稿只写了表情包。"
        first = self.run_(self.p._write(day, update_notes=False, finalize=False))
        self.assertEqual(first, "临时稿只写了表情包。")
        self.assertTrue(self.p.store.is_provisional(day))
        self.run_(self.p.record_owner(Ev(self.owner, "晚上又说了米线")))
        self.provs["b"].text = "全天：表情包和米线。"
        again = self.run_(self.p._write(day, update_notes=False, finalize=False))
        self.assertEqual(again, "临时稿只写了表情包。")
        self.assertEqual(self.provs["b"].calls, 1)
        final = self.run_(self.p._write(day, update_notes=False, finalize=True))
        self.assertEqual(final, "全天：表情包和米线。")
        self.assertFalse(self.p.store.is_provisional(day))
        self.assertIn("米线", self.p.store.read_diary(day))
        self.run_(self.p._write(day, update_notes=False, finalize=True))
        self.assertEqual(self.provs["b"].calls, 2)

    def collect(self, ev, arg=""):
        async def go(): return [r async for r in self.p.show_diary(ev, arg)]
        return self.run_(go())

    def test_command_owner_only_and_generates_today(self):
        self.assertEqual(self.collect(Ev("qq:FriendMessage:222")), [])
        out = self.collect(Ev(self.owner))
        self.assertIn("没跟我聊", out[0].text)
        self.run_(self.p.record_owner(Ev(self.owner, "今天好累")))
        out = self.collect(Ev(self.owner))
        self.assertIn("今天他又喊累。", out[0].text)
        self.assertTrue(out[0]._diary_skip)
        self.assertIn("没有日记", self.collect(Ev(self.owner), "2026-01-01")[0].text)
        self.assertIn("日期这么写", self.collect(Ev(self.owner), "abc")[0].text)

    def collect_phrase(self, ev):
        async def go(): return [r async for r in self.p.read_diary_phrase(ev)]
        return self.run_(go())

    def test_phrase_returns_file_and_is_not_logged(self):
        day = self.m.day_key(self.m.now_cst())
        self.p.store.save_diary(day, "文件里的原文。")
        out = self.collect_phrase(Ev(self.owner, "看看日记"))
        self.assertIn("文件里的原文。", out[0].text)
        self.assertTrue(out[0]._diary_skip)
        self.assertTrue(out[0].text.startswith(f"【{day} 的日记】"))
        self.run_(self.p.record_owner(Ev(self.owner, "看看日记")))
        self.assertEqual(self.p.store.read_log(day), [])
        self.assertEqual(self.collect_phrase(Ev("qq:FriendMessage:222", "看看日记")), [])

    def test_followup_repeats_file_until_normal_chat(self):
        from datetime import timedelta
        day = self.m.day_key(self.m.now_cst())
        self.p.store.save_diary(day, "文件里的原文。")
        self.assertEqual(self.collect_phrase(Ev(self.owner, "我看看今天的")), [])
        self.collect_phrase(Ev(self.owner, "看看日记"))
        for text in ("我看看今天的", "不应该是今天全天的嘛", "难道没有别的要记嘛"):
            out = self.collect_phrase(Ev(self.owner, text))
            self.assertIn("文件里的原文。", out[0].text, text)
            self.assertTrue(out[0]._diary_skip)
        self.run_(self.p.record_owner(Ev(self.owner, "我看看今天的")))
        self.assertEqual(self.p.store.read_log(day), [])
        self.assertEqual(self.collect_phrase(Ev(self.owner, "今天好累")), [])
        self.run_(self.p.record_owner(Ev(self.owner, "今天好累")))
        self.assertEqual([r["text"] for r in self.p.store.read_log(day)], ["今天好累"])
        self.assertEqual(self.collect_phrase(Ev(self.owner, "我看看今天的")), [])
        self.collect_phrase(Ev(self.owner, "看看日记"))
        self.p._diary_follow["at"] = self.m.now_cst() - timedelta(minutes=21)
        self.assertEqual(self.collect_phrase(Ev(self.owner, "我看看今天的")), [])


    # ---------- v0.2 ----------
    def _log(self, day, text, role="owner"):
        import json
        with (self.p.store.logs / f"{day}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": "20:00", "role": role, "text": text}, ensure_ascii=False) + "\n")

    def test_catch_up_writes_missed_days_and_notes(self):
        self.provs["b"].text = "- 我讨厌香菜\n- 我叫他蠢猪\n随便一句废话"
        self._log("2026-01-02", "今天好累")
        self._log("2026-01-02", "我讨厌香菜", role="bot")
        self.run_(self.p._catch_up(include_today=False))
        self.assertIsNotNone(self.p.store.read_diary("2026-01-02"))
        self.assertEqual(self.p.store.read_notes(), "- 我讨厌香菜\n- 我叫他蠢猪")
        self.assertIn("2026-01-02", self.p.store.notes_done())
        calls = self.provs["b"].calls
        self.run_(self.p._catch_up(include_today=False))
        self.assertEqual(self.provs["b"].calls, calls)  # nothing pending -> no new calls

    def test_prune_keeps_logs_without_diary(self):
        self._log("2020-01-01", "很久以前")
        self.p.store.prune_logs(7, self.m.now_cst().date())
        self.assertTrue(self.p.store.read_log("2020-01-01"))

    def test_inject_only_owner_private(self):
        self.p.store.save_notes("- 我讨厌香菜")
        self.p.store.save_diary("2026-01-03", "今天他夸我了。")
        req = SimpleNamespace(system_prompt="人设")
        self.run_(self.p.inject_self(Ev(self.owner), req))
        self.assertIn("我讨厌香菜", req.system_prompt)
        self.assertIn("今天他夸我了", req.system_prompt)
        self.assertIn("不要在聊天里念日记正文", req.system_prompt)
        self.assertIn("不是私藏", req.system_prompt)
        self.assertTrue(req.system_prompt.startswith("人设"))
        other = SimpleNamespace(system_prompt="人设")
        self.run_(self.p.inject_self(Ev("qq:GroupMessage:9"), other))
        self.run_(self.p.inject_self(Ev("qq:FriendMessage:222"), other))
        self.assertEqual(other.system_prompt, "人设")

    def test_clean_notes(self):
        from diarypkg.diary_core import clean_notes
        self.assertEqual(clean_notes("（暂无）"), "")
        self.assertEqual(clean_notes("1. 我爱吃辣\n- 我爱吃辣\n• 我怕黑\n说明文字"), "- 我爱吃辣\n- 我怕黑")

    def test_notes_command(self):
        async def go(ev, arg=""): return [r async for r in self.p.show_notes(ev, arg)]
        self.assertEqual(self.run_(go(Ev("qq:FriendMessage:222"))), [])
        self.assertIn("还没有", self.run_(go(Ev(self.owner)))[0].text)
        self.p.store.save_notes("- 我爱吃辣\n- 我怕黑")
        out = self.run_(go(Ev(self.owner)))[0]
        self.assertIn("2. 我怕黑", out.text); self.assertTrue(out._diary_skip)
        self.assertIn("删掉了：我爱吃辣", self.run_(go(Ev(self.owner), "删 1"))[0].text)
        self.assertEqual(self.p.store.read_notes(), "- 我怕黑")
        self.assertIn("要删第几条", self.run_(go(Ev(self.owner), "删 9"))[0].text)


if __name__ == "__main__":
    unittest.main()
