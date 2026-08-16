#!/usr/bin/env python3
"""Deterministic query correction, candidate ranking, and playback selection."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from pypinyin import lazy_pinyin

from music_providers import MusicProvider, NeteaseProvider, Track, TrackCandidate


LOGGER = logging.getLogger("xiaozhi-music-search")
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_ALIAS_PATH = PROJECT_ROOT / "config" / "music_query_aliases.json"
PREFERENCE_WORDS = {
    "儿歌": "children",
    "儿童版": "children",
    "原唱": "original",
    "翻唱": "cover",
    "cover": "cover",
    "现场版": "live",
    "live": "live",
    "伴奏": "instrumental",
    "dj": "dj",
}
VERSION_MARKERS = {
    "cover": ("cover", "翻唱", "翻自"),
    "live": ("live", "现场", "演唱会"),
    "instrumental": ("伴奏", "instrumental", "纯音乐"),
    "dj": ("dj", "remix", "混音"),
    "children": ("儿歌", "儿童", "少儿", "童谣"),
}
FILLER_PREFIX = re.compile(r"^(?:请)?(?:帮我)?(?:播放|播|放|来一首|来首|我想听|我要听|想听)+")


def _number_from_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)).strip())
    except ValueError:
        return default
    return value if minimum <= value <= maximum else default


def compact_text(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def base_title(value: str) -> str:
    without_versions = re.sub(r"[\(\uff08\[\u3010][^\)\uff09\]\u3011]*[\)\uff09\]\u3011]", "", value)
    return compact_text(without_versions)


def pinyin_text(value: str) -> str:
    return "".join(lazy_pinyin(compact_text(value), errors="ignore"))


def text_similarity(left: str, right: str) -> float:
    left_compact = compact_text(left)
    right_compact = compact_text(right)
    if not left_compact or not right_compact:
        return 0.0
    character_score = SequenceMatcher(None, left_compact, right_compact).ratio()
    left_pinyin = pinyin_text(left_compact)
    right_pinyin = pinyin_text(right_compact)
    pinyin_score = SequenceMatcher(None, left_pinyin, right_pinyin).ratio() if left_pinyin and right_pinyin else 0.0
    return max(character_score, pinyin_score * 0.96)


@dataclass(frozen=True, slots=True)
class MusicQuery:
    raw_query: str
    normalized_query: str
    title: str
    artist: str = ""
    preferences: tuple[str, ...] = ()
    correction_type: str = ""
    correction_from: str = ""

    def variants(self) -> tuple[str, ...]:
        values = [self.normalized_query]
        original = " ".join(part for part in (self.correction_from or self.title, self.artist) if part)
        if original and compact_text(original) != compact_text(self.normalized_query):
            values.append(original)
        return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


class MusicQueryNormalizer:
    def __init__(self, aliases: Mapping[str, str] | None = None):
        self.aliases = {compact_text(key): str(value).strip() for key, value in (aliases or {}).items() if str(value).strip()}

    @classmethod
    def from_env(cls) -> "MusicQueryNormalizer":
        configured = os.getenv("MUSIC_QUERY_ALIASES_PATH", "").strip()
        path = Path(configured).expanduser() if configured else DEFAULT_ALIAS_PATH
        if not path.exists():
            return cls()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            LOGGER.warning("无法加载音乐查询别名 %s：%s", path, exc)
            return cls()
        return cls(payload if isinstance(payload, dict) else {})

    def normalize(self, raw_query: str) -> MusicQuery:
        cleaned = raw_query.strip().replace("《", "").replace("》", "")
        cleaned = FILLER_PREFIX.sub("", cleaned).strip(" ，,.。！!？?")
        preferences: list[str] = []
        for word, preference in PREFERENCE_WORDS.items():
            if word.casefold() in cleaned.casefold():
                preferences.append(preference)
                cleaned = re.sub(re.escape(word), " ", cleaned, flags=re.IGNORECASE)
        cleaned = " ".join(cleaned.split())
        parts = cleaned.split()
        title = cleaned
        artist = ""
        if len(parts) >= 2 and any("\u4e00" <= character <= "\u9fff" for character in "".join(parts[:-1])):
            title = " ".join(parts[:-1])
            artist = parts[-1]
        alias = self.aliases.get(compact_text(title))
        corrected_title = alias or title
        normalized = " ".join(part for part in (corrected_title, artist) if part)
        return MusicQuery(
            raw_query=raw_query,
            normalized_query=normalized,
            title=corrected_title,
            artist=artist,
            preferences=tuple(dict.fromkeys(preferences)),
            correction_type="alias" if alias else "",
            correction_from=title if alias else "",
        )


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    candidate: TrackCandidate
    score: float
    title_similarity: float
    artist_match: str
    reasons: tuple[str, ...]

    def public_dict(self) -> dict[str, Any]:
        return {
            "provider": self.candidate.provider,
            "track_id": self.candidate.track_id,
            "title": self.candidate.title,
            "artist": self.candidate.artist,
            "score": self.score,
            "source_rank": self.candidate.source_rank + 1,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class MusicSearchResult:
    status: str
    query: MusicQuery
    track: Track | None = None
    selected: ScoredCandidate | None = None
    candidates: tuple[ScoredCandidate, ...] = ()
    candidate_count: int = 0
    failures: tuple[dict[str, str], ...] = ()
    rejected: tuple[dict[str, str], ...] = ()
    account_status: str = ""


def score_candidate(query: MusicQuery, candidate: TrackCandidate) -> ScoredCandidate:
    requested_title = compact_text(query.title)
    candidate_title = base_title(candidate.title) or compact_text(candidate.title)
    similarity = text_similarity(requested_title, candidate_title)
    reasons: list[str] = []
    if requested_title and candidate_title == requested_title:
        score = 90.0
        reasons.append("title_exact")
    elif (
        requested_title
        and min(len(requested_title), len(candidate_title)) >= 2
        and min(len(requested_title), len(candidate_title)) / max(len(requested_title), len(candidate_title)) >= 0.65
        and (requested_title in candidate_title or candidate_title in requested_title)
    ):
        score = 72.0
        reasons.append("title_contains")
    else:
        score = similarity * 70
        reasons.append("title_similar")
        if len(requested_title) >= 3 and len(requested_title) == len(candidate_title) and similarity >= 0.78:
            score += 12
            reasons.append("catalog_correction")

    artist_match = "not_requested"
    if query.artist:
        requested_artist = compact_text(query.artist)
        candidate_artist = compact_text(candidate.artist)
        if requested_artist and (requested_artist in candidate_artist or candidate_artist in requested_artist):
            score += 20
            artist_match = "matched"
            reasons.append("artist_matched")
        else:
            score -= 35
            artist_match = "mismatched"
            reasons.append("artist_mismatched")

    combined = compact_text(" ".join((candidate.title, candidate.artist, candidate.album)))
    for version, markers in VERSION_MARKERS.items():
        present = any(compact_text(marker) in combined for marker in markers)
        if not present:
            continue
        if version in query.preferences:
            score += 10
            reasons.append(f"version_{version}_matched")
        elif version in {"cover", "live", "instrumental", "dj"}:
            penalty = 10 if version == "cover" else 15
            score -= penalty
            reasons.append(f"version_{version}_penalty")

    if candidate.provider == "navidrome":
        score += 8
        reasons.append("local_provider")
    if len(compact_text(candidate.title)) > max(24, len(requested_title) * 3):
        score -= 20
        reasons.append("long_title_penalty")
    score -= min(candidate.source_rank, 10)
    return ScoredCandidate(candidate, round(max(0.0, min(score, 100.0)), 2), round(similarity, 4), artist_match, tuple(reasons))


class SmartMusicSearch:
    def __init__(self, providers: Sequence[MusicProvider], timeout: float, normalizer: MusicQueryNormalizer | None = None):
        self.providers = list(providers)
        self.timeout = timeout
        self.normalizer = normalizer or MusicQueryNormalizer.from_env()
        self.candidate_limit = round(_number_from_env("MUSIC_SEARCH_CANDIDATE_LIMIT", 10, 1, 20))
        self.resolve_limit = round(_number_from_env("MUSIC_RESOLVE_CANDIDATE_LIMIT", 5, 1, 10))
        self.auto_score = _number_from_env("MUSIC_AUTO_PLAY_SCORE", 78, 1, 100)
        self.confirm_score = _number_from_env("MUSIC_CONFIRM_SCORE", 60, 1, 100)

    async def search(self, raw_query: str) -> MusicSearchResult:
        query = self.normalizer.normalize(raw_query)
        failures: list[dict[str, str]] = []
        candidates: dict[tuple[str, str], TrackCandidate] = {}
        provider_by_name = {provider.name: provider for provider in self.providers}
        for provider in self.providers:
            for variant in query.variants()[:3]:
                try:
                    found = await asyncio.wait_for(
                        provider.search_candidates(variant, self.candidate_limit),
                        timeout=self.timeout,
                    )
                except asyncio.TimeoutError:
                    failures.append({"provider": provider.name, "reason": "timeout"})
                    break
                except (OSError, RuntimeError) as exc:
                    failures.append({"provider": provider.name, "reason": str(exc)})
                    break
                for candidate in found:
                    candidates.setdefault((candidate.provider, candidate.track_id), candidate)

        ranked = sorted((score_candidate(query, item) for item in candidates.values()), key=lambda item: item.score, reverse=True)
        rejected: list[dict[str, str]] = []
        eligible = [item for item in ranked if item.score >= self.confirm_score]
        for scored in eligible[: self.resolve_limit]:
            provider = provider_by_name.get(scored.candidate.provider)
            if provider is None:
                continue
            try:
                track = await asyncio.wait_for(provider.resolve_candidate(scored.candidate), timeout=self.timeout)
            except asyncio.TimeoutError:
                rejected.append({"provider": provider.name, "track_id": scored.candidate.track_id, "reason": "resolve_timeout"})
                continue
            except (OSError, RuntimeError) as exc:
                rejected.append({"provider": provider.name, "track_id": scored.candidate.track_id, "reason": str(exc)})
                continue
            if track is None:
                rejected.append({"provider": provider.name, "track_id": scored.candidate.track_id, "reason": "unavailable"})
                continue

            account_status = ""
            if track.is_preview and isinstance(provider, NeteaseProvider):
                account = await provider.account_status()
                account_status = str(account.get("status", "unknown"))
                if account_status == "login_required":
                    track = replace(
                        track,
                        access_status="login_required",
                        notice="这首目前只有30秒试听，网易云登录可能已过期，请重新登录。",
                    )
                elif track.access_status == "membership_required":
                    track = replace(track, notice="这首目前只有30秒试听，可能会员权益已过期或需要额外权益。")
                else:
                    track = replace(track, notice="这首目前只提供30秒试听，我先播放试听版。")

            status = "selected" if scored.score >= self.auto_score else "needs_confirmation"
            LOGGER.info(
                "音乐候选决策 query=%r normalized=%r selected=%s/%s score=%.2f status=%s preview=%s",
                raw_query,
                query.normalized_query,
                track.title,
                track.artist,
                scored.score,
                status,
                track.is_preview,
            )
            return MusicSearchResult(
                status=status,
                query=query,
                track=track,
                selected=scored,
                candidates=tuple(ranked[:5]),
                candidate_count=len(ranked),
                failures=tuple(failures),
                rejected=tuple(rejected),
                account_status=account_status,
            )

        return MusicSearchResult(
            status="not_found",
            query=query,
            candidates=tuple(ranked[:5]),
            candidate_count=len(ranked),
            failures=tuple(failures),
            rejected=tuple(rejected),
        )
