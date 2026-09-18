#!/usr/bin/env python3
"""Automatic acceptance tests for the persistent MP3 cache."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from music_cache import MusicCache


def mp3_fixture(label: str, size: int = 32) -> bytes:
    return b"ID3" + label.encode("utf-8") + b"-" * size


class MusicCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.cache_directory = Path(self.temporary_directory.name) / "cache"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def commit(self, cache: MusicCache, query: str, track_id: str, body: bytes):
        output, path = cache.create_temporary_file()
        with output:
            output.write(body)
        return cache.commit(
            path,
            {
                "provider": "test",
                "track_id": track_id,
                "title": f"歌曲{track_id}",
                "artist": "歌手",
                "album": "专辑",
            },
            [query],
        )

    def test_mp3_is_persistent_and_independently_playable(self) -> None:
        body = mp3_fixture("persistent")
        cache = MusicCache(self.cache_directory, max_bytes=1024)
        result = self.commit(cache, "播放测试歌曲", "1", body)
        assert result is not None

        audio_path = Path(result.track.audio_path)
        self.assertEqual(audio_path.suffix, ".mp3")
        self.assertEqual(audio_path.read_bytes(), body)
        self.assertIn("歌手 - 歌曲1", audio_path.name)

        reopened = MusicCache(self.cache_directory, max_bytes=1024)
        cached = reopened.lookup("播放 测试歌曲")
        assert cached is not None
        self.assertEqual(Path(cached.audio_path).read_bytes(), body)
        self.assertEqual(cached.hit_count, 1)

    def test_invalid_non_mp3_response_is_never_cached(self) -> None:
        cache = MusicCache(self.cache_directory, max_bytes=1024)
        result = self.commit(cache, "错误响应", "bad", b"<html>upstream error</html>")
        self.assertIsNone(result)
        self.assertEqual(cache.status()["track_count"], 0)

    def test_lru_eviction_enforces_capacity(self) -> None:
        first = mp3_fixture("first", 10)
        second = mp3_fixture("second", 10)
        cache = MusicCache(self.cache_directory, max_bytes=len(first) + len(second) - 1)
        self.assertIsNotNone(self.commit(cache, "第一首", "1", first))
        result = self.commit(cache, "第二首", "2", second)
        assert result is not None

        self.assertEqual([track.track_id for track in result.evicted], ["1"])
        self.assertIsNone(cache.lookup("第一首"))
        self.assertIsNotNone(cache.lookup("第二首"))
        self.assertLessEqual(int(cache.status()["size_bytes"]), int(cache.status()["max_bytes"]))


if __name__ == "__main__":
    unittest.main()
