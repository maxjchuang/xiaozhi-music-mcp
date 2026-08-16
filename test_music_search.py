#!/usr/bin/env python3
"""Tests for deterministic query correction and candidate decisions."""

from __future__ import annotations

import unittest

from music_providers import Track, TrackCandidate
from music_search import MusicQueryNormalizer, SmartMusicSearch, score_candidate


class FakeCandidateProvider:
    def __init__(self, name: str, candidates: list[TrackCandidate], tracks: dict[str, Track | None]):
        self.name = name
        self.candidates = candidates
        self.tracks = tracks

    async def search_candidates(self, query: str, limit: int = 10) -> list[TrackCandidate]:
        return self.candidates[:limit]

    async def resolve_candidate(self, candidate: TrackCandidate) -> Track | None:
        return self.tracks.get(candidate.track_id)

    async def search(self, query: str, limit: int = 5) -> list[Track]:
        return []


class MusicSearchTests(unittest.IsolatedAsyncioTestCase):
    def test_alias_corrects_common_asr_error(self) -> None:
        query = MusicQueryNormalizer({"世界真是小": "世界真细小"}).normalize("播放世界真是小")
        self.assertEqual(query.title, "世界真细小")
        self.assertEqual(query.normalized_query, "世界真细小")
        self.assertEqual(query.correction_type, "alias")

    def test_explicit_artist_mismatch_is_heavily_penalized(self) -> None:
        query = MusicQueryNormalizer().normalize("安河桥 宋冬野")
        cover = TrackCandidate("netease", "cover", "安河桥（Cover 宋冬野）", "王贰浪")
        original = TrackCandidate("netease", "original", "安河桥", "宋冬野")
        self.assertLess(score_candidate(query, cover).score, 60)
        self.assertGreater(score_candidate(query, original).score, score_candidate(query, cover).score)

    async def test_related_preview_beats_unrelated_full_track(self) -> None:
        candidates = [
            TrackCandidate("netease", "preview", "世界真细小", "黄霍", source_rank=0),
            TrackCandidate("netease", "wrong", "早点早点", "沙一汀EL", source_rank=1),
        ]
        tracks = {
            "preview": Track(
                "netease", "preview", "世界真细小", "黄霍", "https://x/preview.mp3",
                is_preview=True, preview_duration=30, fee=1, payed=0, access_status="membership_required",
            ),
            "wrong": Track("netease", "wrong", "早点早点", "沙一汀EL", "https://x/wrong.mp3"),
        }
        provider = FakeCandidateProvider("netease", candidates, tracks)
        result = await SmartMusicSearch(
            [provider], timeout=1, normalizer=MusicQueryNormalizer({"世界真是小": "世界真细小"})
        ).search("世界真是小")
        self.assertEqual(result.status, "selected")
        self.assertEqual(result.track.track_id, "preview")
        self.assertTrue(result.track.is_preview)

    async def test_unrelated_candidate_is_not_selected(self) -> None:
        candidate = TrackCandidate("netease", "wrong", "[game]ヘッジホッグ（刺猬）歌", "世界补充计划")
        provider = FakeCandidateProvider(
            "netease",
            [candidate],
            {"wrong": Track("netease", "wrong", candidate.title, candidate.artist, "https://x/wrong.mp3")},
        )
        result = await SmartMusicSearch([provider], timeout=1, normalizer=MusicQueryNormalizer()).search("刺界")
        self.assertEqual(result.status, "not_found")
        self.assertIsNone(result.track)

    async def test_one_character_title_does_not_confirm_for_two_character_query(self) -> None:
        candidate = TrackCandidate("netease", "short", "刺", "主人", source_rank=3)
        provider = FakeCandidateProvider(
            "netease",
            [candidate],
            {"short": Track("netease", "short", candidate.title, candidate.artist, "https://x/short.mp3")},
        )
        result = await SmartMusicSearch([provider], timeout=1, normalizer=MusicQueryNormalizer()).search("刺界")
        self.assertEqual(result.status, "not_found")


if __name__ == "__main__":
    unittest.main()
