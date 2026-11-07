"""Per-channel inclusion rules for recurring channel sync.

Standard library only: ``scripts/release_preflight.py`` loads this module on the
release host, where the application dependencies are not installed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

MAX_CONFIG_BYTES = 65536
MAX_CHANNELS = 100
MAX_PATTERNS = 20
MAX_PATTERN_CHARS = 200
# YouTube titles are at most 100 characters; the cap bounds regex work if a
# listing ever returns something longer.
MAX_TITLE_CHARS = 300
MissingChoice = Literal["include", "skip"]
_MISSING_CHOICES = ("include", "skip")
_TOP_LEVEL_KEYS = {"channels"}
_CHANNEL_KEYS = {
    "url",
    "min_duration_seconds",
    "max_duration_seconds",
    "missing_duration",
    "title_include",
    "title_exclude",
    "published_after",
    "missing_published_date",
}


@dataclass(frozen=True)
class ChannelRules:
    min_duration_seconds: int | None = None
    max_duration_seconds: int | None = None
    missing_duration: MissingChoice = "include"
    title_include: tuple[re.Pattern[str], ...] = ()
    title_exclude: tuple[re.Pattern[str], ...] = ()
    published_after: date | None = None
    missing_published_date: MissingChoice = "include"

    def skip_reason(self, *, title: str, duration_seconds: int | None, published: date | None) -> str | None:
        """Return the first rule that excludes an upload, or ``None`` to include it."""
        if self.min_duration_seconds is not None or self.max_duration_seconds is not None:
            if duration_seconds is None:
                if self.missing_duration == "skip":
                    return "missing_duration"
            elif self.min_duration_seconds is not None and duration_seconds < self.min_duration_seconds:
                return "min_duration_seconds"
            elif self.max_duration_seconds is not None and duration_seconds > self.max_duration_seconds:
                return "max_duration_seconds"
        bounded_title = title[:MAX_TITLE_CHARS]
        if self.title_include and not any(pattern.search(bounded_title) for pattern in self.title_include):
            return "title_include"
        if any(pattern.search(bounded_title) for pattern in self.title_exclude):
            return "title_exclude"
        if self.published_after is not None:
            if published is None:
                if self.missing_published_date == "skip":
                    return "missing_published_date"
            elif published < self.published_after:
                return "published_after"
        return None


@dataclass(frozen=True)
class SyncChannel:
    url: str
    rules: ChannelRules = ChannelRules()


def parse_channel_urls(raw_urls: str) -> tuple[str, ...]:
    return tuple(url.strip() for url in raw_urls.split(",") if url.strip())


def entry_published_date(entry: Mapping[str, Any]) -> date | None:
    """Read a UTC publication day from a yt-dlp listing entry, if it has one."""
    for key in ("release_timestamp", "timestamp"):
        value = entry.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                return datetime.fromtimestamp(value, tz=timezone.utc).date()
            except (OverflowError, OSError, ValueError):
                return None
    upload_date = entry.get("upload_date")
    if isinstance(upload_date, str) and re.fullmatch(r"\d{8}", upload_date):
        try:
            return datetime.strptime(upload_date, "%Y%m%d").date()
        except ValueError:
            return None
    return None


def load_sync_channels(raw_urls: str, config_path: str) -> tuple[SyncChannel, ...]:
    """Combine ``CHANNEL_SYNC_URLS`` (rules-free) with the optional rules file.

    Raises ``ValueError`` with a message naming the offending entry and key.
    """
    channels = [SyncChannel(url=url) for url in parse_channel_urls(raw_urls)]
    if config_path:
        channels.extend(parse_channel_sync_config(_read_config(config_path)))
    seen: set[str] = set()
    for channel in channels:
        if channel.url in seen:
            raise ValueError(f"CHANNEL_SYNC_CONFIG: channel {channel.url} is configured more than once")
        seen.add(channel.url)
    if len(channels) > MAX_CHANNELS:
        raise ValueError(f"CHANNEL_SYNC_CONFIG: at most {MAX_CHANNELS} sync channels are allowed")
    return tuple(channels)


def _read_config(config_path: str) -> str:
    path = Path(config_path)
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise ValueError("CHANNEL_SYNC_CONFIG: file exceeds 64 KiB")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise ValueError("CHANNEL_SYNC_CONFIG: file cannot be read as UTF-8 text") from None


def parse_channel_sync_config(raw: str) -> tuple[SyncChannel, ...]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"CHANNEL_SYNC_CONFIG: invalid JSON at line {exc.lineno} column {exc.colno}") from None
    if not isinstance(data, dict):
        raise ValueError('CHANNEL_SYNC_CONFIG: top level must be an object with a "channels" list')
    unknown = sorted(set(data) - _TOP_LEVEL_KEYS)
    if unknown:
        raise ValueError(f"CHANNEL_SYNC_CONFIG: unknown top-level keys: {', '.join(unknown)}")
    entries = data.get("channels")
    if not isinstance(entries, list):
        raise ValueError('CHANNEL_SYNC_CONFIG: "channels" must be a list')
    return tuple(_parse_channel(index, entry) for index, entry in enumerate(entries))


def _parse_channel(index: int, entry: object) -> SyncChannel:
    where = f"CHANNEL_SYNC_CONFIG channels[{index}]"
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: must be an object")
    unknown = sorted(set(entry) - _CHANNEL_KEYS)
    if unknown:
        raise ValueError(f"{where}: unknown keys: {', '.join(unknown)}")
    url = entry.get("url")
    if not isinstance(url, str) or not url.strip() or "," in url:
        raise ValueError(f"{where}: url must be a non-empty string without commas")
    minimum = _duration(where, entry, "min_duration_seconds")
    maximum = _duration(where, entry, "max_duration_seconds")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError(f"{where}: min_duration_seconds is greater than max_duration_seconds")
    has_duration_bound = minimum is not None or maximum is not None
    missing_duration = _missing_choice(where, entry, "missing_duration", required=has_duration_bound)
    published_after = _published_after(where, entry)
    missing_published_date = _missing_choice(
        where, entry, "missing_published_date", required=published_after is not None
    )
    return SyncChannel(
        url=url.strip(),
        rules=ChannelRules(
            min_duration_seconds=minimum,
            max_duration_seconds=maximum,
            missing_duration=missing_duration,
            title_include=_patterns(where, entry, "title_include"),
            title_exclude=_patterns(where, entry, "title_exclude"),
            published_after=published_after,
            missing_published_date=missing_published_date,
        ),
    )


def _duration(where: str, entry: Mapping[str, Any], key: str) -> int | None:
    value: object = entry.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{where}: {key} must be a non-negative integer")
    return value


def _missing_choice(where: str, entry: Mapping[str, Any], key: str, *, required: bool) -> MissingChoice:
    value = entry.get(key)
    if value is None:
        if required:
            raise ValueError(f'{where}: {key} is required ("include" or "skip") when its rule is set')
        return "include"
    if not required:
        raise ValueError(f"{where}: {key} has no effect without its rule")
    if value == "include":
        return "include"
    if value == "skip":
        return "skip"
    raise ValueError(f'{where}: {key} must be "include" or "skip"')


def _published_after(where: str, entry: Mapping[str, Any]) -> date | None:
    value = entry.get("published_after")
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{where}: published_after must be a YYYY-MM-DD date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{where}: published_after must be a YYYY-MM-DD date") from None


def _patterns(where: str, entry: Mapping[str, Any], key: str) -> tuple[re.Pattern[str], ...]:
    value = entry.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not value or len(value) > MAX_PATTERNS:
        raise ValueError(f"{where}: {key} must be a list of 1 to {MAX_PATTERNS} patterns")
    compiled = []
    for position, pattern in enumerate(value):
        if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_PATTERN_CHARS:
            raise ValueError(f"{where}: {key}[{position}] must be a pattern of 1 to {MAX_PATTERN_CHARS} characters")
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            raise ValueError(f"{where}: {key}[{position}] is not a valid regular expression ({exc.msg})") from None
    return tuple(compiled)
