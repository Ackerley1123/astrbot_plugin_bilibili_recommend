"""Recommendation engine with no dependency on AstrBot or its internal search code."""
from __future__ import annotations

import asyncio
import hashlib
import html
import io
import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from PIL import Image, UnidentifiedImageError

DEFAULT_TOPICS = ("JPOP YOASOBI 现场", "动画 新番 PV", "二次元 动画 音乐")
BV_PATTERN = re.compile(r"BV[0-9A-Za-z]{10}")


class RecommendationError(Exception):
    """A user-facing error whose message contains no API response or credential."""


def clean(value: object, limit: int = 160) -> str:
    text = html.unescape(re.sub(r"<[^>]*>", "", str(value or "")))
    return re.sub(r"[\s\x00-\x1f\x7f]+", " ", text).strip()[:limit]


def strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(x.strip() for x in value if isinstance(x, str) and x.strip()))


def video_id(url: object) -> str | None:
    try:
        p = urlparse(str(url))
        if (p.scheme != "https" or p.hostname not in {"www.bilibili.com", "bilibili.com", "m.bilibili.com"}
                or p.username or p.password or p.port not in (None, 443)):
            return None
        match = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10})/?", p.path)
        return match[1] if match else None
    except ValueError:
        return None


def cover_url(value: object) -> str | None:
    url = str(value or "")
    if url.startswith("http://"):
        url = "https://" + url[7:]
    try:
        p = urlparse(url)
        if (len(url) > 2048 or p.scheme != "https" or not (p.hostname or "").endswith(".hdslb.com")
                or p.username or p.password or p.port not in (None, 443)):
            return None
    except ValueError:
        return None
    return url


def bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


@dataclass(frozen=True)
class Settings:
    topics: tuple[str, ...] = DEFAULT_TOPICS
    cooldown_seconds: int = 15
    timeout_seconds: int = 60
    max_results: int = 7
    send_cover: bool = True
    cover_max_bytes: int = 6 * 1024 * 1024
    cache_limit: int = 50
    dedup_limit: int = 100
    max_sessions: int = 500

    @classmethod
    def from_config(cls, config: dict) -> Settings:
        return cls(
            topics=tuple(x for item in strings(config.get("topics")) if (x := clean(item, 80))) or DEFAULT_TOPICS,
            cooldown_seconds=bounded_int(config.get("cooldown_seconds"), 15, 5, 3600),
            timeout_seconds=bounded_int(config.get("timeout_seconds"), 60, 10, 120),
            max_results=bounded_int(config.get("max_results"), 7, 1, 10),
            send_cover=bool(config.get("send_cover", True)),
            cover_max_bytes=bounded_int(config.get("cover_max_mb"), 6, 1, 10) * 1024 * 1024,
            cache_limit=bounded_int(config.get("cache_limit"), 50, 5, 200),
            dedup_limit=bounded_int(config.get("dedup_limit"), 100, 10, 500),
        )


@dataclass(frozen=True)
class Video:
    bvid: str
    title: str
    author: str
    published_at: int
    image_path: str = ""

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}/"


class StateStore:
    """Bounded per-conversation history, without plaintext account/session identifiers."""

    def __init__(self, root: Path, max_sessions: int = 500):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = root / "state.json"
        self.max_sessions = max_sessions
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
                    raise ValueError("invalid state structure")
            except (OSError, ValueError):
                raise RecommendationError("推荐记录无法读取，请管理员检查插件数据目录中的 state.json。") from None
        else:
            data = {"schema_version": 1, "sessions": {}}
        self.data = data
        self.prune()

    def prune(self):
        sessions = self.data["sessions"]
        for key in list(sessions):
            item = sessions[key]
            if not re.fullmatch(r"[0-9a-f]{64}", key) or not isinstance(item, dict):
                sessions.pop(key)
                continue
            seen = item.get("seen", [])
            item["seen"] = [x for x in (seen if isinstance(seen, list) else []) if isinstance(x, str) and BV_PATTERN.fullmatch(x)][-500:]
            for field in ("last_manual", "last_proactive", "updated_at", "topic_index"):
                try:
                    value = float(item.get(field, 0))
                    item[field] = max(0, value) if math.isfinite(value) else 0
                except (ValueError, TypeError, OverflowError):
                    item[field] = 0
        while len(sessions) > self.max_sessions:
            oldest = min(sessions, key=lambda x: sessions[x].get("updated_at", 0))
            sessions.pop(oldest)

    def session(self, scope: str) -> tuple[str, dict]:
        key = hashlib.sha256(scope.encode("utf-8")).hexdigest()
        entry = self.data["sessions"].setdefault(key, {"seen": [], "topic_index": 0})
        entry["updated_at"] = time.time()
        self.prune()
        return key, entry

    def save(self):
        temp = self.path.with_suffix(".json.tmp")
        try:
            temp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
            temp.replace(self.path)
        except OSError:
            raise RecommendationError("推荐记录保存失败，请管理员检查插件数据目录权限和剩余空间。") from None


class PublicClient:
    """Tavily basic search, Bilibili public metadata and bounded public cover download."""

    def __init__(self, root: Path, settings: Settings):
        self.root, self.settings = root, settings

    async def search(self, keyword: str, keys: tuple[str, ...]) -> list[str]:
        keys = strings(keys)
        if not keys:
            raise RecommendationError("请在插件配置中填写 Tavily Key，或启用复用 AstrBot 已配置的 Tavily Key。")
        payload = {"query": f"site:bilibili.com/video/ {keyword}", "max_results": self.settings.max_results,
                   "exclude_domains": ["search.bilibili.com", "m.bilibili.com"],
                   "search_depth": "basic", "topic": "general", "include_answer": False}
        timeout = aiohttp.ClientTimeout(total=min(30, self.settings.timeout_seconds))
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            for key in keys[:3]:
                async with session.post("https://api.tavily.com/search", json=payload,
                                        headers={"Authorization": f"Bearer {key}"}, allow_redirects=False) as response:
                    if response.status in {401, 403, 429, 432, 433}:
                        continue
                    if response.status != 200:
                        raise RecommendationError("搜索服务暂不可用，请稍后再试。")
                    data = await response.json(content_type=None)
                    rows = data.get("results", [])
                    return [str(row["url"]) for row in rows if isinstance(row, dict) and isinstance(row.get("url"), str)]
        raise RecommendationError("搜索密钥不可用或额度不足，请管理员检查 Tavily 配置。")

    async def video(self, bvid: str) -> dict | None:
        if not BV_PATTERN.fullmatch(bvid):
            return None
        timeout = aiohttp.ClientTimeout(total=12)
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers, trust_env=True) as session:
            async with session.get("https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid}, allow_redirects=False) as response:
                if response.status != 200:
                    return None
                result = await response.json(content_type=None)
        data = result.get("data") or {}
        if result.get("code") != 0 or not isinstance(data, dict) or data.get("bvid") != bvid or not clean(data.get("title")):
            return None
        return data

    async def cover(self, value: object, bvid: str) -> str:
        url = cover_url(value)
        if not url or not BV_PATTERN.fullmatch(bvid):
            return ""
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(url, headers={"Referer": "https://www.bilibili.com/", "User-Agent": "Mozilla/5.0"}, allow_redirects=False) as response:
                if response.status != 200:
                    return ""
                parts, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > self.settings.cover_max_bytes:
                        return ""
                    parts.append(chunk)
        data = b"".join(parts)
        try:
            with Image.open(io.BytesIO(data)) as image:
                suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(image.format)
                if not suffix or image.width * image.height > 32_000_000:
                    return ""
                image.verify()
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
            return ""
        cache = self.root / "covers"
        cache.mkdir(parents=True, exist_ok=True)
        path = cache / (bvid + suffix)
        path.write_bytes(data)
        files = [x for x in cache.iterdir() if x.is_file() and re.fullmatch(r"BV[0-9A-Za-z]{10}\.(jpg|png|webp)", x.name)]
        files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        for old in files[self.settings.cache_limit:]:
            old.unlink(missing_ok=True)
        return str(path.resolve())


class RecommendationService:
    def __init__(self, root: Path, settings: Settings, client=None, clock=time.time):
        self.settings, self.clock = settings, clock
        self.store = StateStore(root, settings.max_sessions)
        self.client = client or PublicClient(root, settings)
        self.locks: dict[str, asyncio.Lock] = {}
        self.semaphore = asyncio.Semaphore(2)

    async def recommend(self, scope: str, keyword: str, keys: tuple[str, ...], *, proactive: bool = False) -> Video:
        if not scope:
            raise RecommendationError("无法识别当前会话，请重新发送推荐指令。")
        if len(keyword) > 80:
            raise RecommendationError("关键词过长，请使用80字以内的公开主题、歌手或作品名。")
        if not strings(keys):
            raise RecommendationError("尚未配置 Tavily 搜索密钥，请管理员完成插件配置。")
        key = hashlib.sha256(scope.encode("utf-8")).hexdigest()
        lock = self.locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            raise RecommendationError("这个会话正在挑选视频，请稍等。")
        # Bound the lock map as well as persistent state. Never evict an active lock.
        for old in list(self.locks):
            if len(self.locks) <= self.settings.max_sessions:
                break
            if old != key and not self.locks[old].locked():
                self.locks.pop(old)
        async with lock:
            _, state = self.store.session(scope)
            field = "last_proactive" if proactive else "last_manual"
            cooldown = 900 if proactive else self.settings.cooldown_seconds
            elapsed = self.clock() - float(state.get(field, 0))
            if state.get(field) and elapsed < cooldown:
                raise RecommendationError(f"请再等{max(1, int(cooldown - elapsed + 0.999))}秒后换一个视频。")
            try:
                await asyncio.wait_for(self.semaphore.acquire(), timeout=1)
            except asyncio.TimeoutError:
                raise RecommendationError("当前推荐请求较多，请稍后再试。") from None
            try:
                state[field] = self.clock()
                self.store.save()
                topic = clean(keyword, 80)
                if not topic:
                    index = int(state.get("topic_index", 0)) % len(self.settings.topics)
                    topic = self.settings.topics[index]
                    state["topic_index"] = (index + 1) % len(self.settings.topics)
                video = await asyncio.wait_for(self._fetch(topic, keys, set(state.get("seen", []))), self.settings.timeout_seconds)
                state["seen"] = (state.get("seen", []) + [video.bvid])[-self.settings.dedup_limit:]
                self.store.save()
                return video
            except RecommendationError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError, TypeError, AttributeError):
                raise RecommendationError("本次视频检索未完成，请稍后重试；不会使用未核实的结果。") from None
            finally:
                self.semaphore.release()

    async def _fetch(self, topic: str, keys: tuple[str, ...], seen: set[str]) -> Video:
        urls = await self.client.search(topic, keys)
        candidates = list(dict.fromkeys(b for url in urls if (b := video_id(url)) and b not in seen))
        for bvid in candidates:
            try:
                data = await self.client.video(bvid)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                continue
            if not data or data.get("bvid") != bvid or not clean(data.get("title")):
                continue
            image = ""
            if self.settings.send_cover:
                try:
                    image = await self.client.cover(data.get("pic"), bvid)
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
                    pass
            return Video(bvid, clean(data["title"]), clean((data.get("owner") or {}).get("name"), 80),
                         bounded_int(data.get("pubdate"), 0, 0, 9_999_999_999), image)
        raise RecommendationError("没找到未重复且可核实的新视频，请换个关键词；也可能是B站接口暂时不可用。")
