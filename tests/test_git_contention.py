import asyncio
import threading

from app.api import github


def test_git_jobs_use_dedicated_single_worker():
    async def run():
        names = await asyncio.gather(
            github._run_git_job(lambda: threading.current_thread().name),
            github._run_git_job(lambda: threading.current_thread().name),
        )
        assert names[0].startswith("webagent-git")
        assert names[1] == names[0]

    asyncio.run(run())

def test_git_read_cache_is_stale_while_one_refresh_runs():
    async def run():
        key = ("test-status", "repo")
        github._GIT_READ_CACHE.pop(key, None)
        github._GIT_READ_REFRESHES.pop(key, None)
        release = threading.Event()
        calls = 0

        def loader():
            nonlocal calls
            calls += 1
            if calls > 1:
                release.wait(timeout=2)
            return {"generation": calls}

        first = await github._cached_git_read(key, loader)
        assert first["generation"] == 1
        assert first["_cache"]["stale"] is False

        second = await asyncio.wait_for(
            github._cached_git_read(key, loader), timeout=0.25
        )
        assert second["generation"] == 1
        assert second["_cache"] == {
            "stale": True,
            "refreshing": True,
            "age_ms": second["_cache"]["age_ms"],
        }
        release.set()
        task = github._GIT_READ_REFRESHES.get(key)
        if task is not None:
            await task
        assert github._GIT_READ_CACHE[key][1]["generation"] == 2

    asyncio.run(run())
