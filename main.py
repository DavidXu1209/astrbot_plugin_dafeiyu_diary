"""私人日记插件：只记录主人私聊，每晚写一篇日记存档，主人发 /日记 查看。
补写漏掉的日记；每天整理一份“关于我自己”的笔记；
主人私聊时把自我笔记和最近日记注入到系统提示词里当背景，让机器人的性格和心情能延续。"""
from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .diary_core import (
    DIARY_SYSTEM,
    SELF_SYSTEM,
    DiaryStore,
    build_injection,
    build_prompt,
    build_self_prompt,
    clean_notes,
    day_key,
    now_cst,
    parse_day_arg,
    seconds_until,
)

PLUGIN = "astrbot_plugin_dafeiyu_diary"
SKIP_OWNER = re.compile(r"^/?\s*(日记|自我笔记|reset|new|过稿|拒稿|看稿|撤稿|写说说|写稿|发说说|sid|help|qzone)(\s|$)", re.I)


class DiaryPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self.store = DiaryStore(Path(StarTools.get_data_dir(PLUGIN)))
        self._tasks: list[asyncio.Task] = []
        self._lock = asyncio.Lock()

    # ---------- identity ----------
    @property
    def owner(self) -> str:
        return str(self.config.get("owner_qq", "") or "").strip()

    def _owner_umo(self, event: AstrMessageEvent | None = None) -> str:
        # adapter_id 留空时，从当前事件的 unified_msg_origin 里取平台前缀，
        # 这样不绑定具体适配器实例名。
        adapter = str(self.config.get("adapter_id", "") or "").strip()
        if not adapter and event is not None:
            adapter = event.unified_msg_origin.split(":", 1)[0]
        return f"{adapter}:FriendMessage:{self.owner}"

    def _is_owner_private(self, event: AstrMessageEvent) -> bool:
        return bool(self.owner) and event.unified_msg_origin == self._owner_umo(event)

    # ---------- lifecycle ----------
    async def initialize(self) -> None:
        self._tasks = [asyncio.create_task(self._nightly_loop()),
                       asyncio.create_task(self._startup_catch_up())]

    async def terminate(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with suppress(asyncio.CancelledError, Exception):
                await t

    async def _startup_catch_up(self) -> None:
        # 重启会错过 23:50，启动后稍等一会儿把之前漏写的日记补上。
        await asyncio.sleep(float(self.config.get("catch_up_delay_seconds", 120)))
        await self._catch_up(include_today=False)

    async def _catch_up(self, include_today: bool) -> None:
        try:
            today = now_cst().date()
            for day in self.store.pending_days(today, include_today=include_today):
                await self._write(day)
            if include_today:
                day = today.isoformat()
                if self.store.read_log(day) and day not in self.store.notes_done():
                    await self._update_notes(day)
            self.store.prune_logs(int(self.config.get("raw_retention_days", 7)), today)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never crash the loop
            logger.warning(f"[diary] catch-up failed: {type(exc).__name__}")

    async def _nightly_loop(self) -> None:
        while True:
            await asyncio.sleep(seconds_until(str(self.config.get("write_time", "23:50")), now_cst()))
            await self._catch_up(include_today=True)
            await asyncio.sleep(61)

    # ---------- recording ----------
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=10)
    async def record_owner(self, event: AstrMessageEvent):
        if not self._is_owner_private(event):
            return
        text = (event.message_str or "").strip()
        if text and not SKIP_OWNER.match(text):
            self.store.append("owner", text, now_cst())

    @filter.after_message_sent()
    async def record_reply(self, event: AstrMessageEvent):
        if not self._is_owner_private(event):
            return
        result = event.get_result()
        chain = getattr(result, "chain", None) or []
        parts = [getattr(c, "text", "") for c in chain if getattr(c, "text", None)]
        text = "".join(parts).strip()
        if text and not text.startswith("✅") and not getattr(result, "_diary_skip", False):
            self.store.append("bot", text, now_cst())

    # ---------- give the bot its own continuity ----------
    @filter.on_llm_request()
    async def inject_self(self, event: AstrMessageEvent, req):
        if not self.config.get("inject_enabled", True) or not self._is_owner_private(event):
            return
        try:
            notes = self.store.read_notes()
            diaries = self.store.recent_diaries(int(self.config.get("inject_diary_days", 2)))
            extra = build_injection(notes, diaries, int(self.config.get("inject_diary_chars", 350)))
            if extra:
                req.system_prompt = f"{req.system_prompt or ''}\n\n{extra}"
        except Exception as exc:
            logger.warning(f"[diary] inject failed: {type(exc).__name__}")

    # ---------- model calls ----------
    async def _ask(self, prompt: str, system: str) -> str | None:
        for pid in self.config.get("provider_ids") or []:
            provider = self.context.get_provider_by_id(pid)
            if provider is None:
                continue
            try:
                resp = await provider.text_chat(prompt=prompt, system_prompt=system, contexts=[])
            except Exception as exc:
                logger.warning(f"[diary] provider {pid} failed: {type(exc).__name__}")
                continue
            text = (getattr(resp, "completion_text", "") or "").strip()
            if text:
                return text
        return None

    async def _update_notes(self, day: str) -> None:
        transcript = self.store.transcript(day, int(self.config.get("max_log_chars", 6000)))
        if not transcript:
            return
        old = self.store.read_notes()
        raw = await self._ask(build_self_prompt(old, transcript), SELF_SYSTEM)
        if raw is None:
            return
        new = clean_notes(raw, int(self.config.get("max_self_notes", 30)))
        if new:
            self.store.save_notes(new)
        self.store.mark_notes_done(day)
        logger.info(f"[diary] self notes updated from {day} ({len(new.splitlines())} items)")

    # ---------- writing ----------
    async def _write(self, day: str, update_notes: bool = True) -> str | None:
        async with self._lock:
            existing = self.store.read_diary(day)
            if existing:
                return existing
            transcript = self.store.transcript(day, int(self.config.get("max_log_chars", 6000)))
            if not transcript:
                return None
            text = await self._ask(build_prompt(transcript), DIARY_SYSTEM)
            if not text:
                return None
            self.store.save_diary(day, text)
            logger.info(f"[diary] wrote diary for {day} ({len(text)} chars)")
            if update_notes and day not in self.store.notes_done():
                with suppress(Exception):
                    await self._update_notes(day)
            return text

    # ---------- commands ----------
    @filter.command("日记")
    async def show_diary(self, event: AstrMessageEvent, arg: str = ""):
        if not self._is_owner_private(event):
            return
        event.stop_event()
        today = now_cst().date()
        if arg.strip() in {"列表", "目录"}:
            days = self.store.list_days()
            msg = "写过的日记：\n" + "\n".join(days) if days else "还一篇都没写呢。"
            yield event.plain_result(msg)
            return
        day = parse_day_arg(arg, today)
        if day is None:
            yield event.plain_result("日期这么写：/日记、/日记 昨天、/日记 11-06、/日记 列表")
            return
        text = self.store.read_diary(day)
        if text is None and day <= today.isoformat() and self.store.read_log(day):
            # 今天的日记临时写，晚上再整理自我笔记（那时聊天更完整）。
            text = await self._write(day, update_notes=(day != today.isoformat()))
        elif text is None and day == today.isoformat():
            yield event.plain_result("今天还没跟我聊几句呢，没得写。")
            return
        if text is None:
            yield event.plain_result(f"{day} 没有日记。")
            return
        result = event.plain_result(f"【{day} 的日记】\n{text}")
        result._diary_skip = True  # don't log the diary itself as chat
        yield result

    @filter.command("自我笔记")
    async def show_notes(self, event: AstrMessageEvent, arg: str = ""):
        """主人查看或删除机器人的自我笔记：/自我笔记、/自我笔记 删 3"""
        if not self._is_owner_private(event):
            return
        event.stop_event()
        lines = [l for l in self.store.read_notes().splitlines() if l.strip()]
        parts = (arg or "").split()
        if parts and parts[0] in {"删", "删除"}:
            idx = parts[1] if len(parts) > 1 else ""
            if not idx.isdigit() or not (1 <= int(idx) <= len(lines)):
                yield self._skip(event.plain_result(f"要删第几条？发 /自我笔记 删 编号（1-{len(lines)}）"))
                return
            gone = lines.pop(int(idx) - 1)
            self.store.save_notes("\n".join(lines)) if lines else self.store.notes_path.unlink(missing_ok=True)
            yield self._skip(event.plain_result(f"删掉了：{gone[2:] if gone.startswith('- ') else gone}"))
            return
        if not lines:
            yield self._skip(event.plain_result("还没有自我笔记。"))
            return
        body = "\n".join(f"{i}. {l[2:] if l.startswith('- ') else l}" for i, l in enumerate(lines, 1))
        yield self._skip(event.plain_result(f"【自我笔记】\n{body}\n\n删某条：/自我笔记 删 编号"))

    @staticmethod
    def _skip(result):
        result._diary_skip = True
        return result
