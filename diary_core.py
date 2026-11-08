"""Pure helpers for the private diary (no AstrBot imports, unit-testable)."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
_DATE_ARG = re.compile(r"^(?:(\d{4})-)?(\d{1,2})-(\d{1,2})$")

DIARY_SYSTEM = (
    "用你自己的口吻写私人日记，供主人在私聊里查看。"
    "用第一人称“我”写，提到主人时叫他“他”。用自然的普通话口语，"
    "嘴硬一点也可以，但写下那些平时不好意思说出口的在意。"
    "可以夹一点自己的心理活动，像没说出口的自言自语。"
)
DIARY_RULES = (
    "根据当天私聊写一篇日记。把这一天里和他的主要互动都写进去，"
    "按先后写清他说了什么、我怎么接的，并在旁边带一两句自己的心理活动："
    "当时心里怎么嘀咕、哪里嘴硬、哪里其实在意。可以写成没说出口的自言自语，稍微一点就够，"
    "别大段抒情，也别每句都感叹。"
    "互动少就短；互动多就把主要的几件都写上，大约 400 到 900 字，别为了凑字重复，也别写成流水账。"
    "可以嘴硬，但把在意写出来。少套用告白。"
    "只把有记录的互动当作事实。感受只写我自己的当下心情和期待，不写他的内心。"
    "虚拟日程、模拟穿搭、待办和角色编出的日常都不进入日记，即使它们在聊天中出现。"
    "那部分如果引起了真实互动，可以写当时聊得怎么样，但不要复述虚拟事项。"
    "不编造没聊到的经历、他的内心想法或线下见面。"
    "不要写标题、日期、Markdown 或解释，直接输出日记正文。"
)
DIARY_CHAT_RULE = (
    "【日记】他想看日记时，只能靠 /日记 拿到原文。不要在聊天里念日记正文、续写、改写，"
    "也不要新写一篇来代替。没写的那一天就说还没写，不要把别的日期当成今天，也不要说后面私藏了。"
)

SELF_SYSTEM = "你在帮机器人整理一份关于它自己的笔记。只输出笔记列表本身，不要解释。"
SELF_RULES = (
    "下面是“我”（机器人自己）已有的自我笔记，和今天我跟他的私聊记录。请输出更新后的完整笔记。\n"
    "规则：\n"
    "- 只记“我”在聊天里自己明确说过、表过态的东西：口味和喜好、讨厌的东西、对事情的看法、习惯和小毛病、"
    "口头禅、我给他起的外号、我们之间反复出现的梗。\n"
    "- 不记他的个人信息（那由别的记忆负责），不记一时的情绪，不记角色扮演里编出来的日程、穿搭或经历。\n"
    "- 不记系统和功能相关的事：指令怎么用、发说说的流程、我能不能发语音、能不能看到群消息之类，这些会变，不属于我的性格。\n"
    "- 不记我是 AI、程序、模型、机器之类的话，也不记方言口音、句尾加“来”这种说话习惯。\n"
    "- 不记长相和穿着（那是固定设定）。\n"
    "- 旧笔记默认全部保留。今天说的和旧笔记冲突时，以今天为准，写成“以前……，现在……”。意思重复的合并成一条。\n"
    "- 每条一行，以“- ”开头，用第一人称“我”，一句话说清。最多 30 条，超了就合并或删掉最不重要的。\n"
    "- 今天没有新内容就原样输出旧笔记。旧笔记为空、今天也没有，就只输出：（暂无）"
)
MAX_NOTES = 30


def now_cst() -> datetime:
    return datetime.now(CST)


def day_key(dt: datetime) -> str:
    return dt.astimezone(CST).strftime("%Y-%m-%d")


def parse_day_arg(arg: str, today: date) -> str | None:
    arg = (arg or "").strip()
    if not arg or arg in {"今天", "今日"}:
        return today.isoformat()
    if arg in {"昨天", "昨日"}:
        return (today - timedelta(days=1)).isoformat()
    if arg == "前天":
        return (today - timedelta(days=2)).isoformat()
    m = _DATE_ARG.match(arg)
    if not m:
        return None
    year = int(m.group(1) or today.year)
    try:
        return date(year, int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def is_before_write_time(hhmm: str, now: datetime) -> bool:
    """True when `now` (北京时间) is still before the nightly write clock."""
    try:
        hh, mm = (int(x) for x in hhmm.split(":", 1))
        target_t = time(hh, mm)
    except Exception:
        target_t = time(23, 50)
    now = now.astimezone(CST)
    return (now.hour, now.minute) < (target_t.hour, target_t.minute)


_COMMAND_DIARY = re.compile(r"^/?日记(?:$|列表|目录|昨天|昨日|今天|今日|前天|\d)")
_DIARY_VIEW_VERB = re.compile(
    r"(?:看看|看一下|看下|瞧瞧|瞧一下|念念|念一下|念下|读读|读一下|读下|听听|听一下|"
    r"发给我|发我|查一下|查下|给我看看|给我看|我想看|我要看|想看|看日记|念日记|读日记)"
)
_DATE_IN_TEXT = re.compile(r"(?:\d{4}-)?\d{1,2}-\d{1,2}")


# 看过日记之后，短跟话要回同一篇原文，而不是再交给人设写一版。
FOLLOW_SAME = "*"
_FOLLOW_SEE = re.compile(r"看看|看一下|看下|瞧瞧|瞧一下|给我看|我想看|我要看|念一|念念|再念")
_FOLLOW_MORE = re.compile(
    r"别的要记|没有别的|还有别的|还有吗|还有没|后面的|后面呢|再念|继续念|漏了|没写全|不完整|没念完|私藏"
)
_FOLLOW_COMPLAIN = re.compile(r"不应该|不全|不是今天|不是全天")


def diary_follow_arg(text: str) -> str | None:
    """刚看过日记时，把短跟话收成 show_diary 的参数。

    返回 "" 表示今天，"昨天"/"前天" 表示换一天，FOLLOW_SAME 表示还看刚才那篇。
    不是跟话就返回 None。句子里已经有「日记」的交给 diary_view_arg。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    s = re.sub(r"\s+", "", raw).strip("。！!？?~～啊呀吧呢哈嘛吗")
    if not s or len(s) > 24 or "日记" in s or _COMMAND_DIARY.match(s):
        return None
    see = _FOLLOW_SEE.search(s)
    more = _FOLLOW_MORE.search(s)
    complain = _FOLLOW_COMPLAIN.search(s)
    if not (see or more or complain):
        return None
    if "前天" in s:
        return "前天"
    if "昨天" in s or "昨日" in s:
        return "昨天"
    if "今天" in s or "今日" in s or "全天" in s:
        return ""
    if more or (see and not complain):
        return FOLLOW_SAME
    return None


def diary_view_arg(text: str) -> str | None:
    """若这句话是在要看日记，返回交给 show_diary 的参数；命令形式和闲聊返回 None。"""
    raw = (text or "").strip()
    if not raw:
        return None
    s = re.sub(r"\s+", "", raw).strip("。！!？?~～啊呀吧呢哈")
    if not s or _COMMAND_DIARY.match(s) or "日记" not in s or len(s) > 40:
        return None
    if "好看" in s or re.search(r"写得|写了|为什么|怎么|喜欢|肉麻|里|那句|说过|提到", s):
        return None
    if not _DIARY_VIEW_VERB.search(s):
        return None
    if "列表" in s or "目录" in s:
        return "列表"
    if "前天" in s:
        return "前天"
    if "昨天" in s or "昨日" in s:
        return "昨天"
    m = _DATE_IN_TEXT.search(s)
    if m:
        return m.group(0)
    return ""


def seconds_until(hhmm: str, now: datetime) -> float:
    try:
        hh, mm = (int(x) for x in hhmm.split(":", 1))
        target_t = time(hh, mm)
    except Exception:
        target_t = time(23, 50)
    now = now.astimezone(CST)
    target = datetime.combine(now.date(), target_t, CST)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


class DiaryStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.logs = root / "logs"
        self.diary = root / "diary"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.diary.mkdir(parents=True, exist_ok=True)
        self.notes_path = root / "self_notes.md"
        self.meta_path = root / "notes_meta.json"
        self.diary_meta_path = root / "diary_meta.json"

    def append(self, role: str, text: str, when: datetime) -> None:
        text = (text or "").strip()
        if not text:
            return
        rec = {"t": when.astimezone(CST).strftime("%H:%M"), "role": role, "text": text}
        with (self.logs / f"{day_key(when)}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def read_log(self, day: str) -> list[dict]:
        path = self.logs / f"{day}.jsonl"
        if not path.is_file():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def transcript(self, day: str, max_chars: int) -> str:
        lines = []
        for rec in self.read_log(day):
            who = "他" if rec.get("role") == "owner" else "我"
            lines.append(f"[{rec.get('t', '')}] {who}：{rec.get('text', '')}")
        text = "\n".join(lines)
        # Keep the most recent part when the day is long.
        return text[-max_chars:] if len(text) > max_chars else text

    def diary_path(self, day: str) -> Path:
        return self.diary / f"{day}.md"

    def read_diary(self, day: str) -> str | None:
        p = self.diary_path(day)
        return p.read_text(encoding="utf-8") if p.is_file() else None

    def save_diary(self, day: str, text: str) -> None:
        self.diary_path(day).write_text(text.strip() + "\n", encoding="utf-8")

    def list_days(self, limit: int = 10) -> list[str]:
        return sorted((p.stem for p in self.diary.glob("*.md")), reverse=True)[:limit]

    def log_days(self) -> list[str]:
        days = []
        for p in self.logs.glob("*.jsonl"):
            try:
                days.append(date.fromisoformat(p.stem).isoformat())
            except ValueError:
                continue
        return sorted(days)

    def _read_diary_meta(self) -> dict:
        try:
            data = json.loads(self.diary_meta_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def is_provisional(self, day: str) -> bool:
        days = self._read_diary_meta().get("provisional", [])
        return isinstance(days, list) and day in days

    def set_provisional(self, day: str, provisional: bool) -> None:
        data = self._read_diary_meta()
        days = set(data.get("provisional") or [])
        if provisional:
            days.add(day)
        else:
            days.discard(day)
        data["provisional"] = sorted(days)
        self.diary_meta_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def pending_days(self, today: date, include_today: bool = False) -> list[str]:
        """Days that still need a diary write (oldest first).

        A missing file counts, and so does a same-day draft marked provisional:
        the nightly job rewrites those once the day is complete.
        """
        limit = today.isoformat()
        out = []
        for d in self.log_days():
            if not (d < limit or (include_today and d == limit)):
                continue
            if not self.read_log(d):
                continue
            if self.read_diary(d) is None or self.is_provisional(d):
                out.append(d)
        return out

    def recent_diaries(self, n: int, before: str | None = None) -> list[tuple[str, str]]:
        days = sorted((p.stem for p in self.diary.glob("*.md")), reverse=True)
        if before:
            days = [d for d in days if d <= before]
        out = []
        for d in days[:max(n, 0)]:
            t = self.read_diary(d)
            if t:
                out.append((d, t.strip()))
        return out

    # ----- self notes -----
    def read_notes(self) -> str:
        return self.notes_path.read_text(encoding="utf-8").strip() if self.notes_path.is_file() else ""

    def save_notes(self, text: str) -> None:
        self.notes_path.write_text(text.strip() + "\n", encoding="utf-8")

    def notes_done(self) -> set[str]:
        try:
            return set(json.loads(self.meta_path.read_text(encoding="utf-8")).get("days", []))
        except Exception:
            return set()

    def mark_notes_done(self, day: str) -> None:
        done = sorted(self.notes_done() | {day})[-60:]
        self.meta_path.write_text(json.dumps({"days": done}, ensure_ascii=False), encoding="utf-8")

    def prune_logs(self, keep_days: int, today: date) -> int:
        cutoff = today - timedelta(days=max(keep_days, 1))
        removed = 0
        for p in self.logs.glob("*.jsonl"):
            try:
                d = date.fromisoformat(p.stem)
            except ValueError:
                continue
            # Keep logs that have no final diary yet (missing, or still a draft).
            if (d < cutoff and self.read_diary(d.isoformat()) is not None
                    and not self.is_provisional(d.isoformat())):
                p.unlink()
                removed += 1
        return removed


def build_prompt(transcript: str) -> str:
    return f"{DIARY_RULES}\n\n今天的私聊记录：\n{transcript}"


def build_self_prompt(old_notes: str, transcript: str) -> str:
    return f"{SELF_RULES}\n\n已有的自我笔记：\n{old_notes or '（暂无）'}\n\n今天的私聊记录：\n{transcript}"


def clean_notes(text: str, max_lines: int = MAX_NOTES) -> str:
    """Keep only '- ' bullet lines, deduplicated, capped. Empty string if nothing usable."""
    seen, out = set(), []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line[0] in "-*•·":
            line = line[1:].strip()
        elif re.match(r"^\d+[.、)]\s*", line):
            line = re.sub(r"^\d+[.、)]\s*", "", line)
        else:
            continue
        if not line or "暂无" in line or line in seen:
            continue
        seen.add(line)
        out.append("- " + line)
        if len(out) >= max_lines:
            break
    return "\n".join(out)


def build_injection(notes: str, diaries: list[tuple[str, str]], diary_chars: int = 350) -> str:
    parts = []
    if notes:
        parts.append("【关于我自己】这是我自己记下的笔记，说话做事和它保持一致：\n" + notes)
    if diaries:
        lines = []
        for d, t in diaries:
            t = " ".join(t.split())
            lines.append(f"{d}：{t[:diary_chars]}{'…' if len(t) > diary_chars else ''}")
        parts.append(
            "【我最近的日记】这是节选，只用来记得那几天的心情。省略号是被截断，不是私藏。"
            "不要在聊天里念、改或另写：\n" + "\n".join(lines)
        )
    return "\n\n".join(parts)
