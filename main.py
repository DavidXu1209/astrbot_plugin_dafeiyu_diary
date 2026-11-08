"""私人日记插件：只记录主人私聊，每晚写一篇日记存档，主人发 /日记 或「看看日记」查看原文。
写点前查看当天会先存临时稿，到点按全天记录定稿。补写漏掉的日记；每天整理一份“关于我自己”的笔记；
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
    DIARY_CHAT_RULE,
    DIARY_SYSTEM,
    SELF_SYSTEM,
    DiaryStore,
    build_injection,
    build_prompt,
    build_self_prompt,
    clean_notes,
    day_key,
    FOLLOW_SAME,
    diary_follow_arg,
    diary_view_arg,
    is_before_write_time,
    now_cst,
    parse_day_arg,
    seconds_until,
)

PLUGIN = "astrbot_plugin_dafeiyu_diary"
FOLLOW_WINDOW = 20 * 60  # 看完日记后，这段时间内的短跟话仍回原文
SKIP_OWNER = re.compile(r"^/?\s*(日记|自我笔记|reset|new|过稿|拒稿|看稿|撤稿|写说说|写稿|发说说|sid|help|qzone)(\s|$)", re.I)


class DiaryPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self.store = DiaryStore(Path(StarTools.get_data_dir(PLUGIN)))
        self._tasks: list[asyncio.Task] = []
        self._lock = asyncio.Lock()
        self._diary_follow: dict | None = None

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
        # 看日记的说法走原文回复，不进聊天记录，避免夜里写日记时把“念日记”当成交谈。
        if text and not SKIP_OWNER.match(text) and diary_view_arg(text) is None and not self._is_follow(event, text):
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
            block = "\n\n".join(part for part in (extra, DIARY_CHAT_RULE) if part)
            if block:
                req.system_prompt = f"{req.system_prompt or ''}\n\n{block}"
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
    async def _write(self, day: str, update_notes: bool = True, finalize: bool = True) -> str | None:
        async with self._lock:
            existing = self.store.read_diary(day)
            provisional = self.store.is_provisional(day)
            # 定稿前再问，返回同一篇临时稿；到点 finalize 才用完整记录重写。
            if existing and not (finalize and provisional):
                return existing.strip()
            transcript = self.store.transcript(day, int(self.config.get("max_log_chars", 6000)))
            if not transcript:
                return existing.strip() if existing else None
            text = await self._ask(build_prompt(transcript), DIARY_SYSTEM)
            if not text:
                return existing.strip() if existing else None
            self.store.save_diary(day, text)
            self.store.set_provisional(day, not finalize)
            logger.info(f"[diary] wrote diary for {day} ({len(text)} chars, finalize={finalize})")
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
            yield self._skip(event.plain_result(msg))
            return
        day = parse_day_arg(arg, today)
        if day is None:
            yield self._skip(event.plain_result("日期这么写：/日记、/日记 昨天、/日记 11-06、/日记 列表"))
            return
        self._arm_follow(event, arg)
        text = self.store.read_diary(day)
        provisional = self.store.is_provisional(day)
        # 写点之前的当天日记是临时稿：先给同一篇，到 write_time 再用全天记录定稿。
        before_cutoff = day == today.isoformat() and is_before_write_time(
            str(self.config.get("write_time", "23:50")), now_cst()
        )
        needs_write = self.store.read_log(day) and (
            text is None or (provisional and not before_cutoff)
        )
        if needs_write and day <= today.isoformat():
            text = await self._write(day, update_notes=not before_cutoff, finalize=not before_cutoff)
        elif text is None and day == today.isoformat():
            yield self._skip(event.plain_result("今天还没跟我聊几句呢，没得写。"))
            return
        if text is None or not str(text).strip():
            yield self._skip(event.plain_result(f"{day} 没有日记。"))
            return
        yield self._skip(event.plain_result(f"【{day} 的日记】\n{str(text).strip()}"))

    def _arm_follow(self, event: AstrMessageEvent, arg: str) -> None:
        self._diary_follow = {"umo": event.unified_msg_origin, "at": now_cst(), "arg": arg}

    def _is_follow(self, event: AstrMessageEvent, text: str) -> bool:
        return self._resolve_follow(event, text) is not None

    def _resolve_follow(self, event: AstrMessageEvent, text: str) -> str | None:
        state = self._diary_follow
        if not state or state.get("umo") != event.unified_msg_origin:
            return None
        if (now_cst() - state["at"]).total_seconds() > FOLLOW_WINDOW:
            return None
        kind = diary_follow_arg(text)
        if kind is None:
            return None
        if kind == FOLLOW_SAME:
            return state.get("arg") or ""
        return kind

    def _clear_follow(self, event: AstrMessageEvent) -> None:
        state = self._diary_follow
        if state and state.get("umo") == event.unified_msg_origin:
            self._diary_follow = None

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=20)
    async def read_diary_phrase(self, event: AstrMessageEvent):
        """「看看日记」以及刚看完后的短跟话，直接回文件，不让人设现编。"""
        if not self._is_owner_private(event):
            return
        text = event.message_str or ""
        arg = diary_view_arg(text)
        if arg is None:
            arg = self._resolve_follow(event, text)
        if arg is None:
            self._clear_follow(event)
            return
        async for result in self.show_diary(event, arg):
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
