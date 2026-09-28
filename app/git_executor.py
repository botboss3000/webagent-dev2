"""Process-wide bounded executor for every server-owned Git operation."""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="webagent-git")


async def run_git_job(operation, /, *args, **kwargs):
    loop = asyncio.get_running_loop()
    call = functools.partial(operation, *args, **kwargs)
    return await loop.run_in_executor(_EXECUTOR, call)
