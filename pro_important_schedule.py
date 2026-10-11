"""Independent AEL Pro important-event reminder center and iCalendar export.

The module deliberately performs no network access by itself. Callers provide
an event-feed loader so its event selection and ICS generation can be tested
without Yahoo Finance, BLS, BEA, or the Federal Reserve being reachable.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from threading import Lock
from time import monotonic
from typing import Any, Callable
from zoneinfo import ZoneInfo
import hashlib

ET = ZoneInfo("America/New_York")
CACHE_TTL_SECONDS = 300
_feed_cache: dict[str, Any] = {"ts": 0.0, "value": None}
_feed_lock = Lock()

IMPORTANT_TYPES = {"FOMC", "CPI", "PPI", "PCE", "非农就业", "财报"}
TYPE_LABELS_ZH = {
    "FOMC": "美联储议息会议",
    "CPI": "消费者价格指数（CPI）",
    "PPI": "生产者价格指数（PPI）",
    "PCE": "个人消费支出价格指数（PCE）",
    "非农就业": "非农就业报告",
    "财报": "纳斯达克 100 大型成分股财报",
}


def get_cached_event_feed(loader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Share the expensive live event feed between the schedule page and ICS export."""
    now = monotonic()
    with _feed_lock:
        if _feed_cache["value"] is not None and now - float(_feed_cache["ts"]) < CACHE_TTL_SECONDS:
            return _feed_cache["value"]
    value = loader()
    if not isinstance(value, dict) or not isinstance(value.get("events"), list):
        raise ValueError("日程数据源返回格式异常")
    with _feed_lock:
        _feed_cache["value"] = value
        _feed_cache["ts"] = monotonic()
    return value


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def filter_important_events(
    feed: dict[str, Any],
    *,
    today: date | None = None,
    days: int = 90,
    event_type: str = "ALL",
) -> dict[str, Any]:
    """Return only upcoming important events in a bounded date window."""
    et_today = today or datetime.now(ET).date()
    days = max(1, min(int(days), 365))
    through = et_today + timedelta(days=days)
    requested = (event_type or "ALL").strip()
    if requested not in {"ALL", *IMPORTANT_TYPES}:
        requested = "ALL"
    selected = []
    seen: set[str] = set()
    for raw in feed.get("events", []):
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("type") or "")
        if kind not in IMPORTANT_TYPES or (requested != "ALL" and kind != requested):
            continue
        start = _as_date(raw.get("date"))
        finish = _as_date(raw.get("end_date")) or start
        # A meeting is no longer upcoming once its start date has passed,
        # even if a multi-day meeting's end date is today or later.
        if start is None or finish is None or start < et_today or start > through:
            continue
        row = dict(raw)
        row["date"] = start.isoformat()
        row["end_date"] = finish.isoformat()
        row["type_label_zh"] = TYPE_LABELS_ZH.get(kind, kind)
        row["days_until"] = (start - et_today).days
        row["event_id"] = hashlib.sha256(
            "|".join([kind, start.isoformat(), finish.isoformat(),
                      str(row.get("ticker") or ""), str(row.get("title") or "")]).encode("utf-8")
        ).hexdigest()[:20]
        key = row["event_id"]
        if key in seen:
            continue
        seen.add(key)
        selected.append(row)
    selected.sort(key=lambda x: (x["date"], x.get("type", ""), x.get("title", "")))
    return {
        "ok": True,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "timezone": "America/New_York",
        "days": days,
        "filter": requested,
        "count": len(selected),
        "counts_by_type": {k: sum(1 for e in selected if e.get("type") == k) for k in sorted(IMPORTANT_TYPES)},
        "events": selected,
        "sources": feed.get("sources", []),
        "note": "重要日程独立于宏观研究；官方宏观发布日期与预测财报日期分开标注。浏览器页面关闭后不推送，导出 ICS 可在手机/电脑日历中建立本地提醒。",
    }


def _ics_escape(value: Any) -> str:
    text = str(value or "")
    return text.replace("\\", "\\\\").replace("\r\n", "\\n").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def _fold_ics_line(line: str, limit: int = 73) -> str:
    """Fold long RFC 5545 lines by UTF-8 byte count, with leading-space continuations."""
    output: list[str] = []
    current = ""
    current_bytes = 0
    for char in line:
        width = len(char.encode("utf-8"))
        if current and current_bytes + width > limit:
            output.append(current)
            current = " "
            current_bytes = 1
        current += char
        current_bytes += width
    output.append(current)
    return "\r\n".join(output)


def build_ics(events: list[dict[str, Any]], *, now: datetime | None = None) -> str:
    """Build calendar events with reminders 24h ahead and 1h before timed releases."""
    created = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//AEL//Important Schedule Reminder//ZH",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:AEL 重要日程提醒",
        "X-WR-TIMEZONE:America/New_York",
    ]
    for event in events:
        start = _as_date(event.get("date"))
        if start is None:
            continue
        finish = _as_date(event.get("end_date")) or start
        kind = str(event.get("type") or "")
        event_id = str(event.get("event_id") or hashlib.sha256(repr(event).encode()).hexdigest()[:20])
        title = str(event.get("title") or TYPE_LABELS_ZH.get(kind, "AEL 重要日程"))
        desc_parts = [
            f"类型：{TYPE_LABELS_ZH.get(kind, kind)}",
            f"日期：{start.strftime('%b')} {start.day} {start.year}",
            f"状态：{'预测日期，可能调整' if event.get('certainty') == 'estimated' else '官方日程'}",
        ]
        if event.get("reference_period"):
            desc_parts.append(f"统计期：{event['reference_period']}")
        if event.get("source"):
            desc_parts.append(f"来源：{event['source']}")
        if event.get("source_url"):
            desc_parts.append(f"来源链接：{event['source_url']}")
        description = "\n".join(desc_parts)
        lines.extend(["BEGIN:VEVENT", f"UID:{event_id}@ael-important-schedule", f"DTSTAMP:{created}"])
        has_fixed_release_time = kind in {"CPI", "PPI", "PCE", "非农就业"}
        if has_fixed_release_time:
            dt_local = datetime.combine(start, time(8, 30), tzinfo=ET).astimezone(timezone.utc)
            end_local = dt_local + timedelta(minutes=15)
            lines.extend([
                f"DTSTART:{dt_local.strftime('%Y%m%dT%H%M%SZ')}",
                f"DTEND:{end_local.strftime('%Y%m%dT%H%M%SZ')}",
            ])
        else:
            end_exclusive = finish + timedelta(days=1)
            lines.extend([
                f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{end_exclusive.strftime('%Y%m%d')}",
            ])
        lines.extend([
            f"SUMMARY:{_ics_escape(title)}",
            f"DESCRIPTION:{_ics_escape(description)}",
        ])
        if event.get("source_url"):
            lines.append(f"URL:{_ics_escape(event['source_url'])}")
        lines.extend(["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:AEL 重要日程提醒（提前 24 小时）", "TRIGGER:-P1D", "END:VALARM"])
        if has_fixed_release_time:
            lines.extend(["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:AEL 重要日程提醒（提前 1 小时）", "TRIGGER:-PT1H", "END:VALARM"])
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold_ics_line(x) for x in lines) + "\r\n"
