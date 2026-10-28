"""ExaBase's BEAM prompts are fetched, never committed (see NOTICE.md).

ensure_exabase_prompts(cache_dir) downloads ExaBase's published adapter into cache_dir and checks
its SHA-256 against SOURCE_SHA256, the hash past.dev's verify.py expects. A mismatch raises.
cache_dir must be outside the repo tree (benchmarks/beam/.cache is git-ignored).
"""

import hashlib
import re
from pathlib import Path

import httpx

SOURCE_URL = "https://fabric.so/p/beam-3VjBcqEVRofZyA5CeazeX"
SOURCE_SHA256 = "584b672d957f3e6f75e96f881ec31da22e1039da1df5fbedb53dc6a9145cfc43"
CACHED_NAME = "exabase_beam_adapter.py"


class PromptsHashMismatch(RuntimeError):
    pass


def download_exabase_adapter() -> bytes:
    """The published page links the file through a signed download URL; follow it."""
    page = httpx.get(SOURCE_URL, follow_redirects=True, timeout=30).text.replace("\\u0026", "&")
    link = re.search(r'https://cdn\.fabric\.so/[^"\s]+?\.py\?token=[^"\s\\]+', page)
    if link is None:
        raise RuntimeError(f"Could not find the adapter's download link on {SOURCE_URL}")
    return httpx.get(link.group(0), follow_redirects=True, timeout=30).content


def ensure_exabase_prompts(cache_dir, fetch=download_exabase_adapter) -> Path:
    """Path of the verified ExaBase file in cache_dir, downloading it first if absent."""
    path = Path(cache_dir) / CACHED_NAME
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fetch())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != SOURCE_SHA256:
        raise PromptsHashMismatch(f"{path} hashes to {digest}, expected {SOURCE_SHA256}")
    return path
