#!/usr/bin/env python3
"""
Stage 3 of cron/trend_cluster.py's pipeline: the model's semantic pass over
already-promoted candidates.

Split out of trend_cluster.py to keep both files under the house 300-line
cap - an implementation detail of semantic_pass(), not a second public
surface. cron/trend_cluster.py re-exports semantic_pass() so callers never
need to know this file exists.

The candidates it's handed have ALREADY cleared MIN_TICKETS and
MIN_DISTINCT_SUBJECTS in trend_cluster.py - that gate is not its call to
make. It never reports a count; every count in the final trend dict is
recomputed in trend_cluster_output.py from the merged digest set after this
module returns.

Two stages: Stage A (cron/trend_cluster_embed.py's embed_and_cluster) is the
merge decision, deterministic embedding + cosine similarity - no model call,
no prompt, always re-runnable to the same answer for the same corpus. Stage B
(build_stage_b_prompt/parse_stage_b_narration, cron/trend_cluster_prompt.py)
narrates each surviving group, one prompt per group, capped at
MAX_NARRATION_CALLS. Splitting narration out from the merge decision this
way avoids the old single-prompt design's failure mode: a corpus with 10+
candidates running 50-200+ tickets each didn't fit any one model's context,
so the model was structurally unable to decide a merge whose two halves were
never both in the same call - Stage A's merge decision doesn't have that
problem anymore because it isn't a prompt at all.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from cron.trend_cluster_embed import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_SIMILARITY_THRESHOLD,
    embed_and_cluster,
)
from cron.trend_cluster_prompt import (
    STAGE_B_SYSTEM_PROMPT,
    STRICT_JSON_SUFFIX,
    build_stage_b_prompt,
    parse_stage_b_narration,
)

logger = logging.getLogger(__name__)

# Caps how many merged groups get their own Stage B narration call, so total
# model calls per semantic_pass() stay bounded (1 for Stage A + at most this
# many for Stage B) regardless of how many groups Stage A returns. The
# largest groups by ticket count are narrated first; the rest keep a
# deterministic title rather than triggering an unbounded number of calls.
MAX_NARRATION_CALLS = 8

_FALLBACK_WHY_RELATED = (
    "Grouped because these tickets share the same normalized technical "
    "entities (error codes, product terms, or symptom phrases) within the trend window."
)
_FALLBACK_ACTION = (
    "Route to a senior tech for cross-ticket review before continuing to treat these as isolated tickets."
)


def _fallback_title(candidates: list, indices: list) -> str:
    entities: set = set()
    for i in indices:
        entities |= candidates[i].entities
    top = sorted(entities)[:3]
    label = " / ".join(e.replace("_", " ") for e in top) if top else "ticket cluster"
    return f"Recurring {label} issue"


def _group_to_dict(group: dict, candidates: list) -> dict:
    digests = []
    seen_ids: set = set()
    for i in group["indices"]:
        for d in candidates[i].digests:
            if d.id not in seen_ids:
                seen_ids.add(d.id)
                digests.append(d)
    return {
        "digests": digests,
        "title": group["title"],
        "why_related": group["why_related"],
        "recommended_action": group["recommended_action"],
    }


def _embed_config() -> tuple:
    """Reads the similarity threshold and embedding model id from config,
    defaulting to DEFAULT_SIMILARITY_THRESHOLD / DEFAULT_EMBEDDING_MODEL.
    Follows the existing cfg_get(load_config(), ...) pattern (see
    cron/scheduler_provider.py's cron.provider lookup and this module's own
    _stage_b_model_ids()) rather than adding a new config surface."""
    from hermes_cli.config import cfg_get, load_config

    cfg = load_config()
    threshold = cfg_get(cfg, "cron", "trend", "similarity_threshold", default=DEFAULT_SIMILARITY_THRESHOLD)
    model = cfg_get(cfg, "cron", "trend", "embedding_model", default=DEFAULT_EMBEDDING_MODEL)
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        logger.warning("semantic_pass: cron.trend.similarity_threshold %r is not a number, using default", threshold)
        threshold = DEFAULT_SIMILARITY_THRESHOLD
    return threshold, (model or DEFAULT_EMBEDDING_MODEL)


def _narrate_group(group: dict, candidates: list, model_call) -> bool:
    """Runs one Stage B call for a single group, updating it in place with
    the model's title/why_related/recommended_action. Returns True if the
    call produced usable narration, False if the group's deterministic
    fallback (already set by the caller) should stand."""
    digests = []
    seen_ids: set = set()
    for i in group["indices"]:
        for d in candidates[i].digests:
            if d.id not in seen_ids:
                seen_ids.add(d.id)
                digests.append(d)

    prompt = build_stage_b_prompt(digests, group["title"])
    try:
        raw = model_call(STAGE_B_SYSTEM_PROMPT, prompt)
    except Exception:
        logger.warning("semantic_pass stage B: model_call raised for group %r", group["indices"], exc_info=True)
        return False

    narration = parse_stage_b_narration(raw)
    if narration is None:
        logger.warning("semantic_pass stage B: unparseable/unusable model JSON for group %r", group["indices"])
        return False

    group["title"] = narration["title"]
    group["why_related"] = narration["why_related"] or _FALLBACK_WHY_RELATED
    group["recommended_action"] = narration["recommended_action"] or _FALLBACK_ACTION
    return True


def _run_stage_b(groups: list, candidates: list, model_call) -> list:
    """Fills every group's title/why_related/recommended_action, calling
    the model for at most MAX_NARRATION_CALLS groups (largest by ticket
    count first) and using a deterministic fallback for the rest and for
    any narration call that fails - one Stage B call per narrated group,
    no retry, so the call count stays exactly bounded."""
    for group in groups:
        group["title"] = _fallback_title(candidates, group["indices"])
        group["why_related"] = _FALLBACK_WHY_RELATED
        group["recommended_action"] = _FALLBACK_ACTION

    def group_ticket_count(g: dict) -> int:
        return sum(len(candidates[i].digests) for i in g["indices"])

    narrate_order = sorted(range(len(groups)), key=lambda gi: group_ticket_count(groups[gi]), reverse=True)
    narrate_set = set(narrate_order[:MAX_NARRATION_CALLS])

    calls_made = 0
    for gi in narrate_set:
        calls_made += 1
        _narrate_group(groups[gi], candidates, model_call)

    logger.info(
        "semantic_pass: stage B made %d model call(s) narrating %d of %d group(s) (cap=%d)",
        calls_made,
        len(narrate_set),
        len(groups),
        MAX_NARRATION_CALLS,
    )
    return groups


def semantic_pass(candidates: list, *, model_call=None, now=None, window_days: int = 14) -> list:
    """Stage 3. Takes already-promoted Candidate objects, returns dicts
    with keys {digests, title, why_related, recommended_action} -
    finalize_trend() computes every count from `digests`. model_call is
    (system: str, user: str) -> str, used for Stage B narration only.

    Stage A (embed_and_cluster, cron/trend_cluster_embed.py) always runs
    and decides which candidates merge - deterministic embedding + cosine
    similarity, not gated on model_call being set. `now`/`window_days`
    bound the embedding cache's rolling-window prune (defaults to "now" and
    a 14-day window matching cron/trend_cluster.py's WINDOW_DAYS, callers
    should pass their own values explicitly - see detect_trends()).

    Stage B narrates each surviving group (one prompt per group, capped at
    MAX_NARRATION_CALLS) only when model_call is not None; otherwise every
    group keeps its deterministic title - see _fallback_title()."""
    if not candidates:
        return []

    threshold, embedding_model = _embed_config()
    index_groups = embed_and_cluster(
        candidates,
        now=now or datetime.now(timezone.utc),
        threshold=threshold,
        model=embedding_model,
        window_days=window_days,
    )
    groups = [{"indices": indices, "reason": ""} for indices in index_groups]

    if model_call is not None:
        groups = _run_stage_b(groups, candidates, model_call)
    else:
        for group in groups:
            group["title"] = _fallback_title(candidates, group["indices"])
            group["why_related"] = _FALLBACK_WHY_RELATED
            group["recommended_action"] = _FALLBACK_ACTION

    return [_group_to_dict(g, candidates) for g in groups]


DEFAULT_STAGE_B_MODEL = "claude-sonnet-5"
DEFAULT_STAGE_B_FALLBACK_MODEL = "glm-5.2:cloud"

# Anthropic call gets its own timeout and a small max_tokens cap - Stage B
# only ever produces a title/why_related/recommended_action, never a long
# narrative, so there's nothing to gain from a bigger budget.
_ANTHROPIC_TIMEOUT_SECONDS = 60.0
_ANTHROPIC_MAX_TOKENS = 1024

# Flips true the first time default_model_call() runs with no
# ANTHROPIC_API_KEY, so the "running on the fallback model" notice is
# logged once per process instead of once per Stage B call.
_logged_fallback_notice = False


def _stage_b_model_ids() -> tuple[str, str]:
    """Reads the Anthropic and fallback model ids from config, defaulting
    to DEFAULT_STAGE_B_MODEL / DEFAULT_STAGE_B_FALLBACK_MODEL. Follows the
    existing cfg_get(load_config(), ...) pattern (see
    cron/scheduler_provider.py's cron.provider lookup) rather than adding a
    new config surface."""
    from hermes_cli.config import cfg_get, load_config

    cfg = load_config()
    anthropic_model = cfg_get(cfg, "cron", "trend", "stage_b_model", default=DEFAULT_STAGE_B_MODEL)
    fallback_model = cfg_get(cfg, "cron", "trend", "stage_b_fallback_model", default=DEFAULT_STAGE_B_FALLBACK_MODEL)
    return (anthropic_model or DEFAULT_STAGE_B_MODEL), (fallback_model or DEFAULT_STAGE_B_FALLBACK_MODEL)


def _log_anthropic_key_absent_once() -> None:
    global _logged_fallback_notice
    if _logged_fallback_notice:
        return
    _logged_fallback_notice = True
    logger.info(
        "ANTHROPIC_API_KEY not set; Stage B naming/narration is running on the "
        "fallback model instead of Anthropic."
    )


def _anthropic_model_call(system: str, user: str, *, model: str) -> str:
    """Calls the Anthropic Messages API for Stage B. Never used as a
    default value and never touched unless ANTHROPIC_API_KEY is set -
    importing this module must never construct an Anthropic client.
    Raises on any API error or on a response with no text content;
    Stage B's own retry/fallback (see semantic_pass()) is the only place
    that's allowed to paper over a bad model response."""
    import anthropic

    client = anthropic.Anthropic(timeout=_ANTHROPIC_TIMEOUT_SECONDS)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=_ANTHROPIC_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as exc:
        raise RuntimeError(f"Stage B Anthropic call failed (model={model}): {exc}") from exc

    text = next((block.text for block in response.content if block.type == "text"), None)
    if text is None:
        raise RuntimeError(
            f"Stage B Anthropic call returned no text content "
            f"(model={model}, stop_reason={response.stop_reason})"
        )
    return text


def _fallback_model_call(system: str, user: str, *, model: str = DEFAULT_STAGE_B_FALLBACK_MODEL) -> str:
    """Live AIAgent-backed model_call, used when ANTHROPIC_API_KEY is
    absent. Never used as a default value and never touched by tests -
    importing this module must never construct an AIAgent or need
    network/config. No toolsets, quiet, no memory, no SOUL identity: a
    data transformation call, not Penny speaking. Model/provider match
    board-watcher-001."""
    from run_agent import AIAgent

    agent = AIAgent(
        model=model,
        provider="custom",
        enabled_toolsets=[],
        quiet_mode=True,
        skip_memory=True,
        load_soul_identity=False,
        skip_context_files=True,
        ephemeral_system_prompt=system,
        platform="cron",
    )
    result = agent.run_conversation(user)
    if isinstance(result, str):
        return result
    # run_conversation returns a dict (see AIAgent.run_conversation's type
    # hint), not an object - getattr() on a dict never finds "response" as
    # an attribute, so this used to fall through to str(dict) and hand
    # _parse_model_json an unparseable Python repr instead of the text.
    if isinstance(result, dict):
        return result.get("final_response") or result.get("response") or result.get("text") or str(result)
    return getattr(result, "response", None) or getattr(result, "text", None) or str(result)


def default_model_call(system: str, user: str) -> str:
    """Live model_call for cron wiring to pass explicitly. Resolves at
    call time, purely on ANTHROPIC_API_KEY's presence: key set ->
    Anthropic Messages API (model id from config, default
    DEFAULT_STAGE_B_MODEL); key absent -> the AIAgent/glm-5.2:cloud
    fallback (model id from config, default
    DEFAULT_STAGE_B_FALLBACK_MODEL), logged once. Landing an API key
    later flips this with no code change - nothing here caches which
    branch was used."""
    anthropic_model, fallback_model = _stage_b_model_ids()

    if os.environ.get("ANTHROPIC_API_KEY"):
        return _anthropic_model_call(system, user, model=anthropic_model)

    _log_anthropic_key_absent_once()
    return _fallback_model_call(system, user, model=fallback_model)
