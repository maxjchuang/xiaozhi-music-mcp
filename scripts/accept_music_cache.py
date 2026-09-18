#!/usr/bin/env python3
"""Run the fully automated acceptance suite for server-side music caching."""

from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
if VENV_PYTHON.is_file() and Path(sys.prefix).resolve() != (PROJECT_ROOT / ".venv").resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve())])
sys.path.insert(0, str(PROJECT_ROOT))

TEST_MODULES = (
    "test_music_cache",
    "test_audio_proxy",
    "test_music_mcp_server",
    "test_usage_analytics",
    "test_feishu_sync",
)


def main() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromNames(TEST_MODULES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        print("\n❌ 音乐缓存自动验收失败", file=sys.stderr)
        return 1
    for script in ("test_mcp.py", "test_mcp_pipe.py"):
        completed = subprocess.run([sys.executable, str(PROJECT_ROOT / script)], check=False)
        if completed.returncode:
            print(f"\n❌ {script} 集成验收失败", file=sys.stderr)
            return completed.returncode
    print("\n✅ 音乐缓存自动验收通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
