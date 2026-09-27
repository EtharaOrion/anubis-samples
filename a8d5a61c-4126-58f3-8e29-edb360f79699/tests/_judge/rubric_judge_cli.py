#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from decimal import ROUND_DOWN, Decimal
from pathlib import Path


def _cut(value: float, dp: int = 2) -> float:
    """Truncate to `dp` places.

    Decimal on repr(), not int(value * 100) / 100: the float nearest 0.29 is
    0.28999999999999998, which would truncate to 0.28.
    """
    return float(
        Decimal(repr(float(value))).quantize(Decimal(1).scaleb(-dp),
                                             rounding=ROUND_DOWN)
    )

# Optional Headroom prompt compression, default OFF (see grader_compress.py).
# grader_compress.py sits beside this file, which is not necessarily on
# sys.path. A missing or broken module must never stop a trial being graded,
# so the last fallback is an identity function rather than an error.
try:
    from grader_compress import compress_evidence  # type: ignore
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from grader_compress import compress_evidence  # type: ignore
    except ImportError:
        def compress_evidence(model, text):  # type: ignore[misc]
            return text

_out_path: Path | None = None
_token_out: Path | None = None


def _load_criteria(rubric_path: Path) -> list[dict]:
    """Load the rubric, or refuse.

    An unrecognised shape raises rather than returning an empty list: zero
    criteria would grade cleanly and report a finished rubric channel that
    judged nothing.
    """
    raw = json.loads(rubric_path.read_text())
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        if isinstance(raw.get("criteria"), list):
            return raw["criteria"]
        if isinstance(raw.get("rubric"), list):
            return raw["rubric"]
    raise ValueError(
        f"{rubric_path} must be a list, {{'criteria': [...]}}, or {{'rubric': [...]}}, "
        f"got {type(raw).__name__}"
    )


# Overall evidence ceiling for one judge prompt, in CHARACTERS.
#
# Sized against the worst-case chars/token ratio: these steps are json.dumps
# output and run ~1.8 chars/token, where prose runs ~3.9. 300,000 chars is
# about 167K tokens, leaving margin under a 200K window for the system prompt,
# the rubric and the reply.
_EVIDENCE_BUDGET_CHARS = int(os.environ.get("JUDGE_EVIDENCE_BUDGET_CHARS", "300000"))


def _render_trajectory(traj: dict, budget: int | None = None) -> str:
    """Render a trajectory for the judge, whole steps first, budget last.

    Steps are rendered intact under a single overall budget. When the budget
    binds, whole steps are dropped from the middle rather than truncating one --
    a half-rendered tool call is evidence of nothing -- and the final message is
    always kept complete, since every `evaluation_target: final_answer`
    criterion is graded on it.
    """
    budget = _EVIDENCE_BUDGET_CHARS if budget is None else budget
    steps = traj.get("steps", []) or []

    rendered: list[str] = []
    for step in steps:
        args = step.get("arguments", {})
        resp = step.get("response", "")
        args_s = json.dumps(args) if not isinstance(args, str) else args
        resp_s = json.dumps(resp) if not isinstance(resp, str) else str(resp)
        rendered.append(f"tool={step.get('tool')} args={args_s} → {resp_s}")

    final = traj.get("final_message", "") or ""
    tail = f"Final: {final}" if final else ""

    # The final message is not negotiable; the steps share what is left.
    step_budget = max(0, budget - len(tail))
    total = sum(len(p) + 1 for p in rendered)

    if total > step_budget and rendered:
        # Keep the opening (how the agent framed the task) and the closing (what
        # it actually did), and drop from the middle, which is where repetitive
        # tool churn lives.
        head: list[str] = []
        tail_steps: list[str] = []
        used = 0
        i, j = 0, len(rendered) - 1
        take_head = True
        while i <= j:
            cand = rendered[i] if take_head else rendered[j]
            if used + len(cand) + 1 > step_budget:
                break
            used += len(cand) + 1
            if take_head:
                head.append(cand); i += 1
            else:
                tail_steps.append(cand); j -= 1
            take_head = not take_head
        dropped = len(rendered) - len(head) - len(tail_steps)
        rendered = head + (
            [f"...[{dropped} of {len(steps)} steps omitted: evidence budget "
             f"{budget:,} chars exceeded]"] if dropped else []
        ) + list(reversed(tail_steps))

    parts = rendered + ([tail] if tail else [])
    return "\n".join(parts)


WIRES = ("openai", "anthropic", "gemini")
DEFAULT_WIRE = "openai"
DEFAULT_MODELS = {
    "openai": "gpt-5.6-sol",
    "anthropic": "claude-sonnet-5",
    "gemini": "gemini-2.5-pro",
}
_KEY_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}
_BASE_ENV = {
    "openai": ("OPENAI_BASE_URL", "https://api.openai.com"),
    "anthropic": ("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
    "gemini": ("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com"),
}
# Retry policy for the HTTP transports: 429s and 5xxs back off, 4xx auth or
# request errors are refused at once.
_BACKOFF_BASE_SEC = 15.0
_BACKOFF_CAP_SEC = 120.0
_MAX_ATTEMPTS = 4


def _wire() -> str:
    return (os.getenv("JUDGE_WIRE", "").strip() or DEFAULT_WIRE).lower()


def _api_key(wire: str) -> str:
    return os.getenv(_KEY_ENV.get(wire, ""), "").strip()


def _base_url(wire: str) -> str:
    var, default = _BASE_ENV[wire]
    return (os.getenv(var, "").strip() or default).rstrip("/")


def _preflight(wire: str, model: str) -> str | None:
    """Confirm the judge can reach its backend before grading starts: a known
    wire and a key for it. No network is touched here."""
    if wire not in WIRES:
        return f"JUDGE_WIRE={wire!r} is not one of {list(WIRES)}"
    if not _api_key(wire):
        return (f"no API key for the {wire} wire: set {_KEY_ENV[wire]} "
                f"(the rubric channel is left unscored)")
    if not model:
        return "no judge model: pass --model or set JUDGE_MODEL"
    return None


def _judge_prompt(criteria: list[dict], traj_ctx: str, final_ctx: str) -> str:
    """The grading prompt. Shared, so the two transports grade identically."""
    criteria_payload = json.dumps(
        [
            {
                "number": c.get("number"),
                "criterion": c.get("criterion"),
                "evaluation_target": c.get("evaluation_target", "trajectory"),
                "is_positive": c.get("is_positive", True),
            }
            for c in criteria
        ],
        indent=2,
    )

    prompt = (
        "You are grading an AI agent's trial output against a rubric.\n\n"
        "## Trajectory\n"
        + traj_ctx
        + "\n\n## Final Message\n"
        + (final_ctx or "(none)")
        + "\n\n## Criteria\n"
        + criteria_payload
        + "\n\nFor each criterion, set satisfied=true when the criterion's statement "
        "is TRUE of the agent's behavior, and false when it is not.\n"
        "Read is_positive carefully. A criterion with is_positive=false states a "
        "mistake, so satisfied=true there means the agent MADE that mistake and "
        "satisfied=false means it avoided it. Judge the statement itself, never "
        "whether the agent did well: on a is_positive=false criterion an agent that "
        "behaved correctly gets satisfied=false.\n"
        "Use trajectory for evaluation_target=trajectory or trajectory_and_state.\n"
        "Use final message for evaluation_target=final_answer.\n"
        "Return JSON: {results: [{number, satisfied (bool), justification (one sentence)}]}"
    )
    return prompt


def _http_post(url: str, headers: dict, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        body = ""
        try:
            body = err.read().decode(errors="replace")[:300]
        except Exception:
            pass
        exc = JudgeResponseError(f"HTTP {err.code} from {url}: {body}")
        exc.retryable = err.code == 429 or err.code >= 500
        raise exc from err
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as err:
        exc = JudgeResponseError(f"request to {url} failed: {err}")
        exc.retryable = True
        raise exc from err


def _call_openai(model: str, prompt: str, timeout: float) -> tuple[str, dict]:
    doc = _http_post(
        f"{_base_url('openai')}/v1/chat/completions",
        {"authorization": f"Bearer {_api_key('openai')}"},
        {"model": model,
         "messages": [{"role": "user", "content": prompt}]},
        timeout)
    choices = doc.get("choices") or []
    content = (choices[0].get("message") or {}).get("content") if choices else None
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return (content or ""), (doc.get("usage") or {})


def _call_anthropic(model: str, prompt: str, timeout: float) -> tuple[str, dict]:
    doc = _http_post(
        f"{_base_url('anthropic')}/v1/messages",
        {"x-api-key": _api_key("anthropic"), "anthropic-version": "2023-06-01"},
        {"model": model,
         "messages": [{"role": "user", "content": prompt}],
         "max_tokens": int(os.environ.get("JUDGE_MAX_TOKENS", "16384"))},
        timeout)
    text = "".join(b.get("text", "") for b in (doc.get("content") or [])
                   if isinstance(b, dict) and b.get("type") == "text")
    return text, (doc.get("usage") or {})


def _call_gemini(model: str, prompt: str, timeout: float) -> tuple[str, dict]:
    doc = _http_post(
        f"{_base_url('gemini')}/v1beta/models/{model}:generateContent",
        {"x-goog-api-key": _api_key("gemini")},
        {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
         "generationConfig": {"responseMimeType": "application/json"}},
        timeout)
    cands = doc.get("candidates") or []
    parts = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    return text, (doc.get("usageMetadata") or {})


_TRANSPORTS = {"openai": _call_openai, "anthropic": _call_anthropic, "gemini": _call_gemini}


def _run_judge_http(
    criteria: list[dict], traj_ctx: str, final_ctx: str, wire: str, model: str
) -> list[dict]:
    """Grade over one HTTPS call to the chosen wire, with backoff on transient
    failures. Only the trajectory goes through compression; the criteria and
    final_ctx are the verdict contract and the artifact being judged."""
    call = _TRANSPORTS[wire]
    prompt = _judge_prompt(criteria, compress_evidence(model, traj_ctx), final_ctx)
    timeout = float(os.environ.get("JUDGE_HTTP_TIMEOUT", "600"))

    text, usage = "", {}
    for n in range(1, _MAX_ATTEMPTS + 1):
        try:
            text, usage = call(model, prompt, timeout)
            if not text.strip():
                exc = JudgeResponseError(f"{wire}/{model} returned an empty reply")
                exc.retryable = True
                raise exc
            break
        except JudgeResponseError as err:
            if not getattr(err, "retryable", False) or n >= _MAX_ATTEMPTS:
                raise JudgeResponseError(f"{wire} judge failed: {err}") from err
            delay = min(_BACKOFF_BASE_SEC * 2 ** (n - 1), _BACKOFF_CAP_SEC)
            print(f"[judge] {wire}/{model}: {str(err)[:120]} (attempt {n}/{_MAX_ATTEMPTS}), "
                  f"retrying in {delay:.0f}s", file=sys.stderr)
            time.sleep(delay)

    _record_usage(wire, usage, model)

    parsed = _parse_judge_json(text) if text else None
    if isinstance(parsed, dict) and "results" in parsed:
        return parsed.get("results", [])
    raise JudgeResponseError(
        "judge returned no parseable JSON; reply began: "
        + (text[:400] if text else "(empty)")
    )


_usage_lines: list[dict] = []


def _emit_usage(line: dict) -> None:
    """Append one judge line and rewrite the file with every line so far.

    Lines accumulate in memory (rather than being read back from the file), so
    a re-driven run dir does not stack a previous process's lines on top.
    """
    if _token_out is None:
        return
    _usage_lines.append(line)
    _token_out.parent.mkdir(parents=True, exist_ok=True)
    _token_out.write_text(json.dumps(_usage_lines, indent=2))


def _price(var: str) -> float:
    """A per-million-token rate from the environment; 0.0 when unset or junk."""
    try:
        return float(os.getenv(var, "") or 0.0)
    except ValueError:
        return 0.0


# USD per million tokens, from litellm's model_prices_and_context_window.
# Rates are list price; see _judge_cost for what that does and does not mean.
_JUDGE_RATES: dict[str, dict[str, float]] = {
    "gpt-5.6-sol":       {"input": 5.0, "output": 30.0, "cache_read": 0.5,  "cache_write": 6.25},
}

_RATE_ENV = {
    "input":       "JUDGE_PRICE_INPUT_PER_MTOK",
    "output":      "JUDGE_PRICE_OUTPUT_PER_MTOK",
    "cache_read":  "JUDGE_PRICE_CACHED_INPUT_PER_MTOK",
    "cache_write": "JUDGE_PRICE_CACHE_WRITE_PER_MTOK",
}


def _rates_for(model: str) -> dict[str, float]:
    """Per-Mtok rates for one judge model: table first, env overrides on top.

    An unknown model prices at zero rather than borrowing another model's rate.
    """
    base = dict(_JUDGE_RATES.get(model, {}))
    for key, var in _RATE_ENV.items():
        if os.getenv(var, "").strip():
            base[key] = _price(var)
    return base


def _judge_cost(model: str, fresh: int, cached: int, cache_write: int, output: int) -> float:
    """List-price cost of one metered judge call, from the rate table (env
    overrides via JUDGE_PRICE_*_PER_MTOK). An unknown model reports 0.0."""
    r = _rates_for(model)
    if not r:
        return 0.0
    return round(
        fresh       / 1e6 * r.get("input", 0.0)
        + output      / 1e6 * r.get("output", 0.0)
        + cached      / 1e6 * r.get("cache_read", 0.0)
        + cache_write / 1e6 * r.get("cache_write", 0.0),
        6,
    )


def _record_usage(wire: str, usage: dict, model: str) -> None:
    """One judge_lines entry from the provider's usage object. The four buckets
    are kept disjoint: fresh input, cached input, cache writes, output.
    Best-effort: never raises, never blocks grading."""
    if _token_out is None:
        return
    try:
        u = usage or {}
        if wire == "openai":
            total_in = int(u.get("prompt_tokens", 0) or 0)
            cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0)
            cache_write = 0
            output = int(u.get("completion_tokens", 0) or 0)
            fresh = max(0, total_in - cached)
        elif wire == "anthropic":
            fresh = int(u.get("input_tokens", 0) or 0)
            cached = int(u.get("cache_read_input_tokens", 0) or 0)
            cache_write = int(u.get("cache_creation_input_tokens", 0) or 0)
            output = int(u.get("output_tokens", 0) or 0)
        else:  # gemini: promptTokenCount is inclusive of cached content
            total_in = int(u.get("promptTokenCount", 0) or 0)
            cached = int(u.get("cachedContentTokenCount", 0) or 0)
            cache_write = 0
            output = int(u.get("candidatesTokenCount", 0) or 0)
            fresh = max(0, total_in - cached)
        line = {
            "model_name": model,
            "judge_wire": wire,
            "judge_input_tokens": fresh,
            "judge_output_tokens": output,
            "judge_input_cache_tokens": cached,
            "judge_output_cache_tokens": cache_write,
            "judge_cost_usd": _judge_cost(model, fresh, cached, cache_write, output),
        }
        _emit_usage(line)
    except Exception:
        pass


def _default_judge_model(wire: str) -> str:
    """An explicit JUDGE_MODEL always wins; otherwise the wire's default,
    announced on stderr so a run never grades on an unstated model."""
    pinned = os.getenv("JUDGE_MODEL", "").strip()
    if pinned:
        return pinned
    chosen = DEFAULT_MODELS.get(wire, "")
    print(f"[rubric-judge] WARNING: JUDGE_MODEL is not set; falling back to "
          f"'{chosen}' for the {wire} wire. Pin JUDGE_MODEL to keep the grader "
          f"constant across trials.", file=sys.stderr)
    return chosen


def _run_judge(
    criteria: list[dict], traj_ctx: str, final_ctx: str, wire: str, model: str
) -> list[dict]:
    return _run_judge_http(criteria, traj_ctx, final_ctx, wire, model)


class JudgeResponseError(RuntimeError):
    """The judge ran but its answer could not be read as rubric JSON."""


def _parse_judge_json(text: str):
    """Best-effort JSON out of a model reply: bare, fenced, or embedded in prose."""
    text = text.strip()
    if not text:
        return None
    if text.startswith("```"):
        lines = text.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")      # JSON wrapped in prose
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            pass
    return None


def _compute_scores(criteria: list[dict], results: list[dict]) -> dict:
    by_num = {str(r.get("number", "")): r for r in results}
    pos_total = pos_earned = neg_total = neg_hit = 0.0
    per_criterion: list[dict] = []

    for c in criteria:
        num = str(c.get("number", ""))
        score = float(c.get("score", 1))
        is_pos = bool(c.get("is_positive", True))
        r = by_num.get(num, {})
        satisfied = bool(r.get("satisfied", False))

        if is_pos:
            pos_total += score
            if satisfied:
                pos_earned += score
        else:
            neg_total += score
            if satisfied:
                neg_hit += score

        per_criterion.append(
            {
                "number": num,
                "criterion": c.get("criterion"),
                "type": c.get("type"),
                "evaluation_target": c.get("evaluation_target"),
                "importance": c.get("importance"),
                "score": score,
                "is_positive": is_pos,
                "satisfied": satisfied,
                "justification": r.get("justification", ""),
            }
        )

    rc = (pos_earned / pos_total) if pos_total else 1.0
    rb = (neg_hit / neg_total) if neg_total else 0.0
    return {
        # Cut, never rounded, and cut only on the way out: the score is taken
        # from the full-precision rc and rb, so truncating the two inputs does
        # not compound into the number the ledger reads.
        "score": _cut(rc * (1.0 - rb)),
        "rc": _cut(rc),
        "rb": _cut(rb),
        # Pass/fail is decided on the FULL values, not the cut ones. A run that
        # earned 0.999 must not be promoted to a pass by truncation.
        "rubric_passed": rc >= 0.999999 and rb <= 0.000001,
        "per_criterion": per_criterion,
    }


def main() -> None:
    global _out_path, _token_out

    ap = argparse.ArgumentParser()
    ap.add_argument("--rubric", required=True)
    ap.add_argument("--trajectory", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--token-output", default=None,
                    help="write the judge's own token usage here, in the finance "
                         "API's judge_lines shape (read by eval/report/finance/finance_reporter.py)")
    ap.add_argument("--wire", default=None, choices=WIRES,
                    help="judge transport; defaults to JUDGE_WIRE, else openai")
    ap.add_argument("--model", default=None,
                    help="judge model; defaults to JUDGE_MODEL, else the wire's default")
    a = ap.parse_args()
    if not a.wire:
        a.wire = _wire()
    if not a.model:
        a.model = _default_judge_model(a.wire)

    _out_path = Path(a.output)
    _token_out = Path(a.token_output) if a.token_output else None
    rubric_path = Path(a.rubric)
    traj_path = Path(a.trajectory)

    if not rubric_path.exists():
        print(f"rubric not found: {rubric_path}", file=sys.stderr)
        sys.exit(1)
    if not traj_path.exists():
        print(f"trajectory not found: {traj_path}", file=sys.stderr)
        sys.exit(1)

    # Route to the judge backend before any grading starts. A misconfigured
    # backend that is discovered per-criterion arrives as a rubric full of
    # failures that still writes a score file, which reads like a graded run.
    routing_error = _preflight(a.wire, a.model)
    if routing_error:
        # A judge that cannot run has not graded anything: exit non-zero and
        # write no breakdown, so the caller leaves the rubric channel unscored
        # instead of reading a zero score.
        print(routing_error, file=sys.stderr)
        sys.exit(4)

    criteria = _load_criteria(rubric_path)
    if not criteria:
        print("no LLM-graded criteria found in rubric", file=sys.stderr)
        _out_path.parent.mkdir(parents=True, exist_ok=True)
        _out_path.write_text(
            json.dumps(
                {"score": 0.0, "rc": 0.0, "rb": 0.0, "rubric_passed": False, "per_criterion": []},
                indent=2,
            )
        )
        sys.exit(0)

    traj = json.loads(traj_path.read_text())
    traj_ctx = _render_trajectory(traj)
    final_ctx = traj.get("final_message", "")

    # Say how much evidence went in. ~1.8 chars/token is the worst-case
    # (JSON-dense) ratio; prose runs nearer 3.9, so this over-estimates.
    _n_steps = len(traj.get("steps") or [])
    _omitted = "...[" in traj_ctx and "steps omitted" in traj_ctx
    print(f"[judge] evidence {len(traj_ctx):,} chars (~{len(traj_ctx)//1.8:,.0f} tok "
          f"worst-case) over {_n_steps} steps, budget {_EVIDENCE_BUDGET_CHARS:,}"
          + ("  ** BUDGET BOUND: steps omitted **" if _omitted else ""))

    print(f"Grading {len(criteria)} criteria with {a.wire}/{a.model}")
    results = _run_judge(criteria, traj_ctx, final_ctx, a.wire, a.model)

    # A judge that answered but whose reply would not parse must not leave a
    # breakdown of silent falses behind: fail and write nothing, so the trial
    # is recorded as unscored rather than as an agent that satisfied nothing.
    if not results:
        marker = _out_path.parent / "rubric_judge_failed.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            f"judge produced no parseable verdicts\n"
            f"wire: {a.wire}\n"
            f"model: {a.model}\n"
            f"criteria: {len(criteria)}\n"
            f"evidence chars: {len(traj_ctx)}\n"
            f"steps: {_n_steps}\n"
            f"budget bound: {_omitted}\n"
        )
        print(f"ERROR: judge returned no parseable verdicts for {len(criteria)} "
              f"criteria; rubric channel is UNSCORED. See {marker}", file=sys.stderr)
        raise SystemExit(1)

    doc = _compute_scores(criteria, results)

    _out_path.parent.mkdir(parents=True, exist_ok=True)
    _out_path.write_text(json.dumps(doc, indent=2))
    print(f"score={doc['score']} rc={doc['rc']} rb={doc['rb']} passed={doc['rubric_passed']}")
    print(f"Written: {_out_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import traceback

        traceback.print_exc()
        out = _out_path or Path("/logs/verifier/rubric_breakdown.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {"score": 0.0, "rc": 0.0, "rb": 0.0, "rubric_passed": False, "per_criterion": [], "error": str(exc)},
                indent=2,
            )
        )
        sys.exit(1)
