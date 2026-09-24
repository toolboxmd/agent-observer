"""Sourced list-price estimates over native counter semantics. Stdlib only.

Every estimate comes from a dated, sourced offline price schedule (JSON)
with explicit USD-per-million-token rates per counter. Nothing is guessed:
unknown models, unsupported semantics, missing rates for nonzero counters,
invalid rates, unknown cache-write TTL and ambiguous long-context tiers
stay unknown, never zero. Pricing is per response under its native
semantics; an undifferentiated token total is never priced and counters
are never averaged.

Native reported cost_usd and subscription quota readings are separate and
are never treated as a list-price estimate.
"""

from __future__ import annotations

import json
import math
import os
from datetime import date

BUCKETS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
           "output_tokens", "reasoning_output_tokens")

DEFAULT_SCHEDULE_PATH = os.path.join(os.path.dirname(__file__), "prices.json")


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def validate_schedule(data: dict) -> dict:
    """Validate a price schedule, fail closed. Returns the same dict."""
    if not isinstance(data, dict):
        raise ValueError("price schedule must be an object")
    for key in ("source_url", "models"):
        if key not in data:
            raise ValueError(f"price schedule missing {key}")
    url = data["source_url"]
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise ValueError("price schedule needs an http(s) source_url")
    as_of = data.get("as_of") or data.get("effective_date")
    try:
        valid_date = isinstance(as_of, str) and date.fromisoformat(as_of).isoformat() == as_of
    except ValueError:
        valid_date = False
    if not valid_date:
        raise ValueError("price schedule needs an as_of/effective_date")
    if data.get("unit", "USD per million tokens") != "USD per million tokens":
        raise ValueError("price schedule unit must be USD per million tokens")
    if data.get("currency", "USD") != "USD":
        raise ValueError("price schedule currency must be USD")
    models = data["models"]
    if not isinstance(models, dict):
        raise ValueError("price schedule models must be an object")
    for model, entry in models.items():
        if not isinstance(model, str) or not model:
            raise ValueError("price schedule model ids must be nonempty strings")
        if not isinstance(entry, dict):
            raise ValueError(f"price schedule entry for {model} must be an object")
        sems = entry.get("semantics")
        if not isinstance(sems, list) or not sems or not all(
                isinstance(s, str) and s for s in sems):
            raise ValueError(
                f"price schedule entry for {model} needs a nonempty semantics list")
        rates = entry.get("rates")
        if not isinstance(rates, dict):
            raise ValueError(
                f"price schedule entry for {model} needs a rates object")
        for bucket, rate in rates.items():
            if bucket not in BUCKETS:
                raise ValueError(
                    f"price schedule entry for {model} has unknown bucket {bucket}")
            if isinstance(rate, dict):
                # TTL-keyed rates require matching native cache-write evidence.
                for ttl, sub in rate.items():
                    if not isinstance(ttl, str) or not ttl:
                        raise ValueError(
                            f"price schedule TTL key for {model}/{bucket} invalid")
                    if not _is_num(sub) or sub < 0:
                        raise ValueError(
                            f"price schedule rate for {model}/{bucket}/{ttl} invalid")
                continue
            if not _is_num(rate) or rate < 0:
                raise ValueError(
                    f"price schedule rate for {model}/{bucket} invalid")
        threshold = entry.get("long_context_threshold")
        if threshold is not None:
            if not isinstance(threshold, int) or isinstance(threshold, bool) \
                    or threshold <= 0:
                raise ValueError(
                    f"price schedule threshold for {model} invalid")
            long_rates = entry.get("long_context_rates")
            if long_rates is not None:
                if not isinstance(long_rates, dict):
                    raise ValueError(
                        f"price schedule long_context_rates for {model} invalid")
                for bucket, rate in long_rates.items():
                    if bucket not in BUCKETS:
                        raise ValueError(
                            f"price schedule long rate for {model} unknown bucket")
                    if not _is_num(rate) or rate < 0:
                        raise ValueError(
                            f"price schedule long rate for {model}/{bucket} invalid")
    return data


def load_schedule(path: str | None = None) -> dict:
    """Load and validate a schedule. None loads the bundled default."""
    target = path or DEFAULT_SCHEDULE_PATH
    with open(target, encoding="utf-8") as fh:
        data = json.load(fh)
    return validate_schedule(data)


def _entry_rates(entry: dict, response: dict) -> tuple[dict | None, str | None]:
    """Select base or long-context rates for one response.

    Returns (rates, reason). A None rates means the tier is ambiguous or
    the long-context total is unpriced, so the response stays unknown.
    """
    threshold = entry.get("long_context_threshold")
    if threshold is None:
        return entry.get("rates", {}), None
    # Tier is decided by input size; an unknown input cannot pick a tier.
    size = response.get("input_tokens")
    if size is None:
        return None, "ambiguous long-context tier (unknown input)"
    try:
        over = int(size) > int(threshold)
    except (TypeError, ValueError):
        return None, "ambiguous long-context tier"
    if not over:
        return entry.get("rates", {}), None
    long_rates = entry.get("long_context_rates")
    if not isinstance(long_rates, dict):
        return None, "long-context tier unpriced"
    return long_rates, None


def _rate_for(rates: dict, bucket: str) -> tuple[float | None, str | None]:
    """Rate for one bucket, or (None, reason) when unpriceable."""
    if bucket not in rates:
        return None, f"missing rate for {bucket}"
    rate = rates[bucket]
    if isinstance(rate, dict):
        return None, f"unknown cache-write TTL for {bucket}"
    if not _is_num(rate) or rate < 0:
        return None, f"invalid rate for {bucket}"
    return float(rate), None


def price_response(response: dict, schedule: dict) -> tuple[float | None, str]:
    """Price one response. Returns (cost_usd or None, reason).

    Cost is in USD. A None cost always carries a reason naming the first
    fail-closed condition: unknown model, unsupported semantics, unknown
    or inconsistent counters, missing/invalid/TTL-ambiguous rates, or an
    ambiguous/unpriced long-context tier. Zero is returned only when every
    priced component is known and zero.
    """
    model = response.get("model")
    entry = (schedule.get("models") or {}).get(model) if model else None
    if entry is None:
        return None, "unknown model"
    sem = response.get("semantics") or "unknown"
    if sem not in (entry.get("semantics") or []):
        return None, f"unsupported semantics {sem}"
    rates, tier_reason = _entry_rates(entry, response)
    if rates is None:
        return None, tier_reason or "ambiguous tier"
    # All five native buckets must be known; unknown stays unknown, never
    # zero, because an absent counter could hide priced usage.
    counters = {}
    for bucket in BUCKETS:
        value = response.get(bucket)
        if bucket == "reasoning_output_tokens" and value is None \
                and sem.startswith(("codex:input_includes_cached", "claude:input_excludes_cache")) \
                and _is_num(rates.get("output_tokens")) \
                and rates.get("output_tokens") == rates.get("reasoning_output_tokens"):
            # Inclusive output prices identically without knowing its split.
            # This local decomposition does not alter the measured counter.
            counters[bucket] = 0
            continue
        if value is None:
            return None, f"unknown counter {bucket}"
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None, f"invalid counter {bucket}"
        counters[bucket] = value
    total = response.get("total_tokens")
    # The harness total is never priced from; it only guards the subset
    # decomposition below (cached subset of input, reasoning subset of
    # output) against inconsistent native rows.
    _ = total
    if sem.startswith("codex:input_includes_cached"):
        cached = counters["cached_input_tokens"]
        reason = counters["reasoning_output_tokens"]
        if cached > counters["input_tokens"]:
            return None, "inconsistent counters (cached above input)"
        if reason > counters["output_tokens"]:
            return None, "inconsistent counters (reasoning above output)"
        parts = {
            "input_tokens": counters["input_tokens"] - cached,
            "cached_input_tokens": cached,
            "cache_write_input_tokens": counters["cache_write_input_tokens"],
            "output_tokens": counters["output_tokens"] - reason,
            "reasoning_output_tokens": reason,
        }
    elif sem.startswith("claude:input_excludes_cache"):
        if counters["reasoning_output_tokens"] > counters["output_tokens"]:
            return None, "inconsistent counters (reasoning above output)"
        parts = {
            "input_tokens": counters["input_tokens"],
            "cached_input_tokens": counters["cached_input_tokens"],
            "cache_write_input_tokens": counters["cache_write_input_tokens"],
            "output_tokens": counters["output_tokens"]
            - counters["reasoning_output_tokens"],
            "reasoning_output_tokens": counters["reasoning_output_tokens"],
        }
    elif sem == "opencode:input_excludes_cache,reasoning_separate":
        parts = dict(counters)
    else:
        return None, f"unsupported semantics {sem}"
    cost = 0.0
    for bucket, amount in parts.items():
        if amount == 0:
            continue
        if bucket == "cache_write_input_tokens" and isinstance(rates.get(bucket), dict):
            split = {ttl: response.get(f"cache_write_{ttl}_tokens") for ttl in ("5m", "1h")}
            if not all(type(n) is int and n >= 0 for n in split.values()):
                return None, "unknown cache-write TTL for cache_write_input_tokens"
            if sum(split.values()) != amount:
                return None, "inconsistent cache-write TTL counters"
            for ttl, count in split.items():
                rate = rates[bucket].get(ttl)
                if count and (not _is_num(rate) or rate < 0):
                    return None, f"missing cache-write rate for {ttl}"
                if count:
                    cost += count * rate / 1_000_000.0
            continue
        rate, reason = _rate_for(rates, bucket)
        if rate is None:
            return None, reason or f"missing rate for {bucket}"
        cost += amount * rate / 1_000_000.0
    return cost, "priced"


def price_scope(rows: list, schedule: dict) -> dict:
    """Price an already selected response scope, per response.

    Returns priced/unpriced coverage with a partial subtotal kept separate
    from the complete total: estimated_cost_usd_total is present only when
    every live response prices; otherwise only the partial subtotal of the
    priced subset is reported and the remainder stays explicitly unknown.
    """
    live = [r for r in rows if not r.get("is_overlap")]
    by_key: dict = {}
    priced_n = 0
    partial = 0.0
    unpriced: dict = {}
    details = []
    for row in live:
        cost, reason = price_response(dict(row), schedule)
        key = (row.get("harness"), row.get("model"), row.get("effort"),
               row.get("semantics") or "unknown")
        cell = by_key.setdefault(key, {"responses": 0, "priced": 0,
                                       "unpriced": 0, "cost": 0.0,
                                       "reasons": {}})
        cell["responses"] += 1
        if cost is None:
            cell["unpriced"] += 1
            cell["reasons"][reason] = cell["reasons"].get(reason, 0) + 1
            unpriced[reason] = unpriced.get(reason, 0) + 1
        else:
            cell["priced"] += 1
            cell["cost"] += cost
            priced_n += 1
            partial += cost
        details.append({"response_id": row.get("response_id"),
                        "cost_usd": cost, "reason": reason})
    by_model = []
    for (harness, model, effort, sem), cell in sorted(
            by_key.items(),
            key=lambda kv: (kv[0][1] or "", kv[0][0] or "", kv[0][2] or "")):
        by_model.append({"harness": harness, "model": model,
                         "basis": (schedule["models"].get(model) or {}).get("basis"),
                         "source_url": (schedule["models"].get(model) or {}).get(
                             "source_url", schedule.get("source_url")),
                         "effort": effort, "semantics": sem,
                         "responses": cell["responses"],
                         "priced_responses": cell["priced"],
                         "unpriced_responses": cell["unpriced"],
                         "estimated_cost_usd": cell["cost"] if cell["unpriced"] == 0
                         else None,
                         "estimated_cost_usd_partial": cell["cost"],
                         "unpriced_reasons": cell["reasons"]})
    complete = priced_n == len(live)
    out = {
        "schedule_source": schedule.get("source_url"),
        "basis": schedule.get("basis", "API list-price equivalent; not subscription spend"),
        "schedule_as_of": schedule.get("as_of") or schedule.get("effective_date"),
        "responses": len(live),
        "priced_responses": priced_n,
        "unpriced_responses": len(live) - priced_n,
        "estimated_cost_usd_partial": partial,
        "estimated_cost_usd_total": partial if complete else None,
        "complete": complete,
        "by_model": by_model,
        "unpriced_reasons": unpriced,
    }
    return out
