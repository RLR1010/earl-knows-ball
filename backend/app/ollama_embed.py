"""Load-balanced Ollama embedding client (one model copy per GPU).

Two Ollama instances run side by side, each pinned to its own GPU and keeping
snowflake-arctic-embed2 resident (keep_alive=-1) so embedding calls never pay
a cold-model load:

    GPU 0  ->  http://localhost:11434   (ollama.service)
    GPU 1  ->  http://localhost:11435   (ollama-gpu1.service)

This module spreads requests across the two instances (least in-flight first,
round-robin on ties so sequential work alternates GPUs) and fails over to the
other instance if one is down or unreachable. Batch helpers split the work
across both instances concurrently (~2x throughput) with fallback to a single
instance if one side fails.

Usage:
    from app.ollama_embed import embed_sync, embed_async, embed_batch_sync, embed_batch_async
"""
import asyncio
import threading

import httpx

SERVERS = ["http://localhost:11434", "http://localhost:11435"]
MODEL = "snowflake-arctic-embed2"
EMBED_PATH = "/api/embeddings"
BATCH_PATH = "/api/embed"

_lock = threading.Lock()
_inflight = [0, 0]
_rr = 0  # round-robin tie-breaker so sequential calls alternate GPUs


def _pick_server() -> int:
    global _rr
    with _lock:
        idx = min(
            range(len(SERVERS)),
            key=lambda i: (_inflight[i], (_rr + i) % len(SERVERS)),
        )
        _inflight[idx] += 1
        _rr += 1
        return idx


def _release(idx: int) -> None:
    with _lock:
        _inflight[idx] = max(0, _inflight[idx] - 1)


def _body(prompt: str) -> dict:
    return {"model": MODEL, "prompt": prompt, "keep_alive": -1}


def _batch_body(texts: list[str]) -> dict:
    return {"model": MODEL, "input": texts, "keep_alive": -1}


def embed_sync(query: str, timeout: float = 60.0) -> list[float]:
    """Embed a single query using the sync client (chat / enrichment path).

    Raises RuntimeError only if both instances fail.
    """
    import requests

    last_err = None
    for _ in range(len(SERVERS)):
        idx = _pick_server()
        try:
            resp = requests.post(
                f"{SERVERS[idx]}{EMBED_PATH}", json=_body(query), timeout=timeout
            )
            resp.raise_for_status()
            return resp.json()["embedding"]
        except Exception as e:  # noqa: BLE001 - fail over to the other instance
            last_err = e
        finally:
            _release(idx)
    raise RuntimeError(f"All embedding servers failed: {last_err}")


async def embed_async(
    query: str, client: httpx.AsyncClient | None = None, timeout: float = 60.0
) -> list[float]:
    """Embed a single query asynchronously (article ingestion path)."""
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=timeout)
    try:
        last_err = None
        for _ in range(len(SERVERS)):
            idx = _pick_server()
            try:
                resp = await client.post(f"{SERVERS[idx]}{EMBED_PATH}", json=_body(query))
                resp.raise_for_status()
                return resp.json()["embedding"]
            except Exception as e:  # noqa: BLE001 - fail over
                last_err = e
            finally:
                _release(idx)
        raise RuntimeError(f"All embedding servers failed: {last_err}")
    finally:
        if own:
            await client.aclose()


def embed_batch_sync(texts: list[str], timeout: float = 180.0) -> list[list[float]]:
    """Embed many texts synchronously, split across both GPUs concurrently.

    Preserves input order. If one instance fails, its half is retried on the
    other, so a single healthy GPU still completes the whole batch.
    """
    if not texts:
        return []
    half = (len(texts) + 1) // 2
    chunks = [texts[:half], texts[half:]]
    out: list[list[list[float]] | None] = [None, None]

    def _run(idx: int) -> None:
        chunk = chunks[idx]
        if not chunk:
            out[idx] = []
            return
        try:
            resp = httpx.post(
                f"{SERVERS[idx]}{BATCH_PATH}",
                json=_batch_body(chunk),
                timeout=timeout,
            )
            resp.raise_for_status()
            out[idx] = resp.json()["embeddings"]
        except Exception:  # noqa: BLE001 - handled by failover below
            out[idx] = None

    threads = [
        threading.Thread(target=_run, args=(0,)),
        threading.Thread(target=_run, args=(1,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Failover: retry failed halves on the surviving instance
    for idx in (0, 1):
        if out[idx] is None and chunks[idx]:
            other = 1 - idx
            resp = httpx.post(
                f"{SERVERS[other]}{BATCH_PATH}",
                json=_batch_body(chunks[idx]),
                timeout=timeout,
            )
            resp.raise_for_status()
            out[idx] = resp.json()["embeddings"]

    return out[0] + out[1]


async def embed_batch_async(texts: list[str], timeout: float = 180.0) -> list[list[float]]:
    """Embed many texts asynchronously, split across both GPUs concurrently."""
    if not texts:
        return []
    half = (len(texts) + 1) // 2
    chunks = [texts[:half], texts[half:]]
    out: list[list[list[float]] | None] = [None, None]

    async def _run(idx: int) -> None:
        chunk = chunks[idx]
        if not chunk:
            out[idx] = []
            return
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{SERVERS[idx]}{BATCH_PATH}", json=_batch_body(chunk)
                )
                resp.raise_for_status()
                out[idx] = resp.json()["embeddings"]
        except Exception:  # noqa: BLE001 - handled by failover below
            out[idx] = None

    await asyncio.gather(_run(0), _run(1))

    for idx in (0, 1):
        if out[idx] is None and chunks[idx]:
            other = 1 - idx
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{SERVERS[other]}{BATCH_PATH}", json=_batch_body(chunks[idx])
                )
                resp.raise_for_status()
                out[idx] = resp.json()["embeddings"]

    return out[0] + out[1]
