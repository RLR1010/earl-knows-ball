"""
Context processor: reranks and deduplicates retrieved context before sending to DeepSeek.

Uses a cross-encoder (sentence-transformers) for relevance reranking:
  - cross-encoder/ms-marco-MiniLM-L-6-v2 — tiny 80MB model, runs on CPU ~20ms per pair

For deduplication, reuses the cross-encoder embeddings to compare chunk similarity.
All processing is local — no API calls, no token costs.
"""
import logging
from typing import Optional

import numpy as np

logger = logging.getLogger("earl.context_processor")

CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
DEDUP_THRESHOLD = 0.85  # lower than embedding-based since cross-encoder scores are different

# Lazy-loaded singleton
_reranker: Optional = None


def _get_reranker():
    global _reranker
    if _reranker is None:
        logger.info(f"Loading cross-encoder: {CROSS_ENCODER_MODEL}")
        from sentence_transformers import CrossEncoder
        _reranker = CrossEncoder(CROSS_ENCODER_MODEL, device="cpu")
    return _reranker


def _score_chunks(question: str, chunks: list[str]) -> list[float]:
    """Score each chunk against the question using the cross-encoder.
    Returns a list of relevance scores (higher = more relevant).
    """
    model = _get_reranker()
    pairs = [(question, chunk) for chunk in chunks]
    scores = model.predict(pairs, show_progress_bar=False)
    return scores.tolist() if hasattr(scores, "tolist") else list(scores)


def _chunk_context(context: str) -> list[dict]:
    """Split a flat context string into labeled chunks based on section headers.
    
    Returns list of { "label": str, "text": str } preserving the original structure.
    """
    lines = context.split("\n")
    chunks = []
    current_label = "GENERAL"
    current_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Detect section headers
        if stripped.startswith("TEAM:"):
            if current_lines:
                chunks.append({"label": current_label, "text": "\n".join(current_lines)})
            current_label = stripped[:40]  # e.g. "TEAM: Green Bay Packers (GB)"
            current_lines = [stripped]
        elif stripped.startswith("PLAYER:"):
            if current_lines:
                chunks.append({"label": current_label, "text": "\n".join(current_lines)})
            current_label = stripped[:40]  # e.g. "PLAYER: Josh Jacobs (RB)"
            current_lines = [stripped]
        elif stripped.startswith("SEASON:") or stripped.startswith("2025 PLAYOFFS"):
            if current_lines:
                chunks.append({"label": current_label, "text": "\n".join(current_lines)})
            current_label = stripped[:40]
            current_lines = [stripped]
        elif stripped.startswith("RELEVANT ARTICLES"):
            if current_lines:
                chunks.append({"label": current_label, "text": "\n".join(current_lines)})
            current_label = stripped[:40]
            current_lines = [stripped]
        elif stripped.startswith("---") and "Article" in stripped:
            # Article separator within RELEVANT ARTICLES section
            current_lines.append(stripped)
        else:
            current_lines.append(stripped)

    if current_lines:
        chunks.append({"label": current_label, "text": "\n".join(current_lines)})

    # If we ended up with a single giant chunk, just return it as-is
    if len(chunks) <= 1:
        return [{"label": "CONTEXT", "text": context}]

    return chunks


async def process_context(question: str, raw_context: str) -> str:
    """
    Main entry point: rerank and deduplicate the raw context.
    
    1. Split into labeled chunks
    2. Cross-encoder scores each chunk against the question
    3. Rerank by relevance
    4. Deduplicate near-identical chunks
    5. Reassemble into final context string
    """
    if not raw_context.strip():
        return raw_context

    chunks = _chunk_context(raw_context)

    # If only one chunk, skip processing
    if len(chunks) <= 1:
        return raw_context

    # Score each chunk against the question using cross-encoder
    chunk_texts = [c["text"] for c in chunks]
    scores = _score_chunks(question, chunk_texts)

    # Pair scores with chunks
    scored = list(zip(scores, range(len(chunks)), chunks))
    # Sort by relevance score (descending)
    scored.sort(key=lambda x: x[0], reverse=True)

    # Deduplicate: skip chunks too similar to a higher-ranked one
    # Use cross-encoder pairwise scoring for dedup
    kept_idxs = []
    kept_texts = []

    for score, i, chunk in scored:
        text = chunk["text"]
        
        # Check against already-kept chunks using cross-encoder
        is_duplicate = False
        if kept_texts:
            # Score the new chunk against all kept chunks
            pairs = [(text, kt) for kt in kept_texts]
            try:
                model = _get_reranker()
                pair_scores = model.predict(pairs, show_progress_bar=False)
                if hasattr(pair_scores, "tolist"):
                    pair_scores = pair_scores.tolist()
                for ps in pair_scores:
                    # Cross-encoder scores are logits — higher means more relevant/similar
                    if float(ps) > DEDUP_THRESHOLD:
                        is_duplicate = True
                        break
            except Exception:
                pass

        if not is_duplicate:
            kept_idxs.append(i)
            kept_texts.append(text)

    # Reassemble in ranked order (most relevant first)
    kept_set = set(kept_idxs)
    result_parts = [
        chunk["text"] for _, i, chunk in scored if i in kept_set
    ]

    compressed = "\n\n".join(result_parts)

    logger.info(
        f"Context processor: {len(chunks)} chunks → {len(kept_idxs)} kept "
        f"(cross-encoder reranked + deduped)"
    )

    return compressed
