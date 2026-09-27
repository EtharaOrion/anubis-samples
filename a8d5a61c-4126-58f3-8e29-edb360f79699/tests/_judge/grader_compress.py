#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List

# Tri-state probe: None = not yet attempted, True/False = the result. One-shot,
# so a missing install costs one stderr line per process rather than one per
# judged criterion.
_HEADROOM_AVAILABLE = None  # type: Any
_compress = None  # type: Any
_CompressConfig = None  # type: Any

# Cumulative and process-wide. stats() hands the caller the numbers so it can
# put them in its own run record without this module owning a file format.
_STATS = {
    "calls_compressed": 0,
    "calls_skipped": 0,
    "calls_refused": 0,
    "tokens_before": 0,
    "tokens_after": 0,
    "tokens_saved": 0,
}

_WARNED = set()  # type: set


def _warn_once(key, msg):
    # type: (str, str) -> None
    if key in _WARNED:
        return
    _WARNED.add(key)
    sys.stderr.write("[grader_compress] {}\n".format(msg))


def _probe():
    # type: () -> bool
    global _HEADROOM_AVAILABLE, _compress, _CompressConfig
    if _HEADROOM_AVAILABLE is not None:
        return _HEADROOM_AVAILABLE
    try:
        from headroom import compress, CompressConfig  # type: ignore

        _compress = compress
        _CompressConfig = CompressConfig
        _HEADROOM_AVAILABLE = True
        # Without this line an installed-but-inert integration is
        # indistinguishable from an uninstalled one: the no-saving path below
        # is silent by design, so "no output" would mean either. Printing on a
        # successful probe splits those two cases apart.
        import headroom as _hr  # noqa: F401

        _warn_once(
            "active",
            "active (headroom {})".format(getattr(_hr, "__version__", "?")),
        )
    except Exception as exc:
        _warn_once(
            "import",
            "headroom not importable, compression disabled "
            "(grading proceeds uncompressed): {}".format(exc),
        )
        _HEADROOM_AVAILABLE = False
    return _HEADROOM_AVAILABLE


def _truthy(value):
    # type: (Any) -> bool
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def enabled():
    # type: () -> bool
    """Default OFF. Compression is opted into, never inherited."""
    return _truthy(os.environ.get("GRADER_HEADROOM_ENABLED"))


def _float_env(name, default):
    # type: (str, float) -> float
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _int_env(name, default):
    # type: (str, int) -> int
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _model_hint(model):
    # type: (Any) -> Any
    """Give Headroom a model id it can size tokens for.

    This only steers token COUNTING -- the gate and the target ratio -- so an
    approximate family match is enough and a wrong guess cannot corrupt output.
    The judge models (e.g. `gpt-5.6-sol`) are not public tokenizer ids.

    gpt-*/o* pass through, where Headroom's own OpenAI tokenizers already
    match. Anything else is sized as gpt-4o, which is closer than failing the
    lookup and falling back to naive string length.
    """
    name = (model or "").strip().lower()
    if name.startswith("gpt") or name.startswith("o1") or name.startswith("o3"):
        return model
    return "gpt-4o"


def _has_cache_control(messages):
    # type: (Any) -> bool
    """True if ANY message carries a cache_control breakpoint.

    The interlock described in the module docstring. Compressing a cached
    prefix trades a 0.10x cache read for a 1.0x fresh read plus a 1.25x
    rewrite -- strictly worse than doing nothing, at any compression ratio
    Headroom achieves. A cached conversation is never compressed.
    """
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        if "cache_control" in message:
            return True
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "cache_control" in block:
                    return True
    return False


_STUB_RE = re.compile(
    r"^\s*\[\d+\s+(?:lines?|items?|messages?)\s+compressed"
    r"(?:\s+to\s+\d+)?\.?\s*(?:Retrieve\s+more)?[^\]]*\]\s*$",
    re.IGNORECASE,
)


def _is_retrieval_stub(text):
    # type: (Any) -> bool
    """True if Headroom replaced the content with a retrieve-me placeholder.

    Such a stub (e.g. "[120 lines compressed to 0. Retrieve more: hash=...]")
    is not a summary: Headroom deletes the content and leaves a handle for a
    `headroom_retrieve` tool. Every grader call here is a single shot to a judge
    that has no tools, so a stub would have the judge grade evidence it never
    saw. Any stub is refused and the original text is sent; real compressed
    prose does not match this pattern and passes through.
    """
    return isinstance(text, str) and bool(_STUB_RE.match(text))


def _content_is_stub(message):
    # type: (Any) -> bool
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if _is_retrieval_stub(content):
        return True
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and _is_retrieval_stub(block.get("text")):
                return True
    return False


def _flatten_text_blocks(message):
    # type: (Any) -> Any
    """Collapse a pure-text-block message to bare-string content.

    Headroom's text/JSON compressors only engage on string content, so
    block-shaped text messages are flattened to a string first.

    Returned UNCHANGED when content is already a string, is empty, or holds
    ANY non-plain-text block -- tool_use, tool_result, image, thinking, or a
    text block carrying cache_control/citations. Those encode structure the
    call depends on; splitting a tool_use/tool_result pair earns a 400
    upstream, not a smaller prompt.
    """
    if not isinstance(message, dict):
        return message
    content = message.get("content")
    if not isinstance(content, list) or not content:
        return message
    parts = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            return message
        if set(block.keys()) - {"type", "text"}:
            return message
        parts.append(block.get("text") or "")
    flat = dict(message)
    flat["content"] = "\n".join(parts)
    return flat


def _config(protect_recent):
    # type: (int) -> Any
    return _CompressConfig(
        compress_user_messages=True,
        # The system message carries the JSON verdict contract. Crushing it is
        # how you get a judge that returns prose and a criterion that scores
        # None after three retries.
        compress_system_messages=False,
        protect_recent=protect_recent,
        min_tokens_to_compress=_int_env("GRADER_HEADROOM_MIN_TOKENS", 500),
        target_ratio=_float_env("GRADER_HEADROOM_TARGET_RATIO", 0.4),
    )


def _log(before, after, saved):
    # type: (int, int, int) -> None
    pct = (100.0 * saved / before) if before else 0.0
    sys.stderr.write(
        "[grader_compress] {} -> {} tokens (saved {}, {:.1f}%)\n".format(
            before, after, saved, pct
        )
    )


def _no_saving_once(result):
    # type: (Any) -> None
    """Report a compressor that ran and declined to shrink anything.

    A trajectory under the library's internal size gate comes back unchanged.
    That is a legitimate outcome, not a fault; naming it distinguishes it from
    a failed install, which is also silent.
    """
    before = int(getattr(result, "tokens_before", 0) or 0)
    _warn_once(
        "nosaving",
        "ran but saved nothing at {} tokens (input under the library's size "
        "gate); sending uncompressed".format(before),
    )


def compress_messages(model, messages):
    # type: (Any, List[Dict[str, Any]]) -> List[Dict[str, Any]]
    """Compress a chat-shaped message list. Returns it unchanged on any doubt.

    For the paths that hand the judge a role-shaped list. System messages are
    never touched, so the verdict contract survives and the evidence in the
    user turn is what gets crushed.

    GRADER_HEADROOM_PROTECT_RECENT defaults to 0 here (Headroom's own default
    is 2): grader calls are one-shot, so protecting a tail of 2 would protect
    the only message carrying evidence. Raise it to 2 for a multi-turn grader.
    """
    if not enabled() or not messages:
        return messages
    if _has_cache_control(messages):
        _warn_once(
            "cache",
            "messages carry cache_control -- compression stood down "
            "(compressing a cached prefix costs more than it saves)",
        )
        _STATS["calls_skipped"] += 1
        return messages
    if not _probe():
        _STATS["calls_skipped"] += 1
        return messages

    try:
        config = _config(_int_env("GRADER_HEADROOM_PROTECT_RECENT", 0))
        flattened = [_flatten_text_blocks(m) for m in messages]
        result = _compress(flattened, model=_model_hint(model), config=config)
    except Exception as exc:
        _warn_once(
            "compress",
            "compress() raised, sending uncompressed: {!r}".format(exc),
        )
        _STATS["calls_skipped"] += 1
        return messages

    saved = int(getattr(result, "tokens_saved", 0) or 0)
    out = getattr(result, "messages", None)
    if saved <= 0 or not isinstance(out, list) or len(out) != len(flattened):
        _no_saving_once(result)
        _STATS["calls_skipped"] += 1
        return messages
    if any(_content_is_stub(m) for m in out):
        _warn_once(
            "stub",
            "compression returned a retrieval stub, not text -- refused and "
            "sent uncompressed (a one-shot judge cannot call headroom_retrieve)",
        )
        _STATS["calls_refused"] += 1
        return messages

    # Restore the original block-shaped message wherever compression was a
    # no-op, so flattening never alters the wire shape of anything Headroom did
    # not actually rewrite.
    merged = [
        new if new != flat else original
        for original, flat, new in zip(messages, flattened, out)
    ]

    before = int(getattr(result, "tokens_before", 0) or 0)
    after = int(getattr(result, "tokens_after", 0) or 0)
    _STATS["calls_compressed"] += 1
    _STATS["tokens_before"] += before
    _STATS["tokens_after"] += after
    _STATS["tokens_saved"] += saved
    _log(before, after, saved)
    return merged


def compress_evidence(model, text):
    # type: (Any, Any) -> Any
    """Compress one blob of trajectory evidence. Returns it unchanged on doubt.

    The rubric judge does not send a conversation -- it assembles one prompt
    string out of a scoring contract, the trajectory, the agent's final
    message, and the criteria. Only the trajectory is compressible evidence,
    so the call site hands that fragment here rather than the assembled prompt:
    crushing the contract breaks the verdict format and crushing the final
    message compresses the artifact under judgement.

    protect_recent is pinned to 0 rather than read from the environment: there
    is no conversation tail to protect inside a single blob, and letting an
    operator raise it would silently switch this function off.
    """
    if not enabled() or not isinstance(text, str) or not text.strip():
        return text
    if not _probe():
        _STATS["calls_skipped"] += 1
        return text

    wrapped = [{"role": "user", "content": text}]
    try:
        result = _compress(
            wrapped, model=_model_hint(model), config=_config(0)
        )
    except Exception as exc:
        _warn_once(
            "compress",
            "compress() raised, sending uncompressed: {!r}".format(exc),
        )
        _STATS["calls_skipped"] += 1
        return text

    saved = int(getattr(result, "tokens_saved", 0) or 0)
    out = getattr(result, "messages", None)
    if saved <= 0 or not isinstance(out, list) or len(out) != 1:
        _no_saving_once(result)
        _STATS["calls_skipped"] += 1
        return text
    first = out[0]
    if not isinstance(first, dict) or not isinstance(first.get("content"), str):
        _STATS["calls_skipped"] += 1
        return text
    if _is_retrieval_stub(first["content"]):
        _warn_once(
            "stub",
            "compression returned a retrieval stub, not text -- refused and "
            "sent uncompressed (a one-shot judge cannot call headroom_retrieve)",
        )
        _STATS["calls_refused"] += 1
        return text

    before = int(getattr(result, "tokens_before", 0) or 0)
    after = int(getattr(result, "tokens_after", 0) or 0)
    _STATS["calls_compressed"] += 1
    _STATS["tokens_before"] += before
    _STATS["tokens_after"] += after
    _STATS["tokens_saved"] += saved
    _log(before, after, saved)
    return first["content"]


def stats():
    # type: () -> Dict[str, int]
    """Process-wide counters, copied so a caller cannot mutate them."""
    return dict(_STATS)


def reset_stats():
    # type: () -> None
    for key in _STATS:
        _STATS[key] = 0
