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
import sqlite3
from datetime import date, datetime, timezone

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
    if not isinstance(url, str) or not url.startswith(
            ("https://", "http://", "file://")):
        raise ValueError("price schedule needs an http(s) or file source_url")
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


def t3_rates_path(home: str | None = None) -> str:
    """The T3 LiteLLM rate table: $T3CODE_HOME (or home) plus the default."""
    base = home or os.environ.get("T3CODE_HOME") \
        or os.path.expanduser("~/.t3")
    return os.path.join(base, "userdata", "usage-model-rates.json")


# Module cache for the multi-megabyte T3 rate table, keyed by identity.
_T3_CACHE: tuple | None = None


def _read_t3_document(path: str) -> tuple[dict, int | None]:
    """The T3 rate document plus its fetch time, cached by file identity."""
    global _T3_CACHE
    try:
        st = os.stat(path)
    except OSError:
        raise ValueError(f"T3 rate table not found at {path}")
    identity = (os.path.abspath(path), st.st_mtime_ns, st.st_size)
    if _T3_CACHE is not None and _T3_CACHE[0] == identity:
        return _T3_CACHE[1], _T3_CACHE[2]
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not isinstance(
            data.get("document"), dict):
        raise ValueError("T3 rate table has no document object")
    _T3_CACHE = (identity, data["document"], data.get("fetchedAtMs"))
    return data["document"], data.get("fetchedAtMs")


_REGION_PREFIXES = ("us.", "eu.", "au.", "jp.", "global.")
_PRICED_MODES = {"chat": 0, "completion": 1, "responses": 2}


def _t3_candidates(document: dict, model: str) -> list:
    """T3 rate keys that can price one observer model id, best first."""
    wanted = model.lower()
    scored = []
    for key, entry in document.items():
        if not isinstance(entry, dict):
            continue
        mode = entry.get("mode")
        if mode not in _PRICED_MODES:
            continue
        if key == model:
            rank = 0
        elif isinstance(key, str) and key.lower() == wanted:
            rank = 1
        elif isinstance(key, str):
            core = key.lower().split("/")[-1].split(".")[-1]
            if core != wanted:
                continue
            rank = 2
        else:
            continue
        region = 1 if key.lower().startswith(_REGION_PREFIXES) else 0
        scored.append((rank, region, _PRICED_MODES[mode], len(key), key))
    return [key for _, _, _, _, key in sorted(scored)]


def _t3_cost(entry: dict, name: str) -> float | None:
    value = entry.get(name)
    return value * 1_000_000.0 if _is_num(value) and value >= 0 else None


def _t3_convert(model: str, semantics: set, key: str,
                entry: dict) -> dict | None:
    """One observer schedule entry from a T3 LiteLLM rate row.

    Returns None when the row cannot price the model fail-closed
    (missing input or output cost): the caller then keeps the bundled
    entry or leaves the model unknown. LiteLLM per-token costs become
    USD per million; the 1-hour cache-write price feeds the ``1h`` TTL
    leg and the 200k-token prices feed the long-context tier.
    """
    rates: dict = {}
    base = _t3_cost(entry, "input_cost_per_token")
    output = _t3_cost(entry, "output_cost_per_token")
    if base is None or output is None:
        return None
    rates["input_tokens"] = base
    rates["output_tokens"] = output
    reasoning = _t3_cost(entry, "output_cost_per_reasoning_token")
    rates["reasoning_output_tokens"] = \
        reasoning if reasoning is not None else output
    read = _t3_cost(entry, "cache_read_input_token_cost")
    if read is not None:
        rates["cached_input_tokens"] = read
    write = _t3_cost(entry, "cache_creation_input_token_cost")
    write_1h = _t3_cost(entry, "cache_creation_input_token_cost_above_1hr")
    if write is not None:
        # Claude reports 5m and 1h cache writes separately; a flat rate
        # would price 1h writes at 5m. Any Claude use needs the split, and
        # other semantics' nonzero cache writes then stay unknown.
        if any(s.startswith("claude:") for s in semantics):
            ttl = {"5m": write}
            if write_1h is not None:
                ttl["1h"] = write_1h
            rates["cache_write_input_tokens"] = ttl
        else:
            rates["cache_write_input_tokens"] = write
    converted = {"semantics": sorted(semantics),
                 "rates": rates,
                 "t3_key": key,
                 "basis": "T3 LiteLLM rate table; list-price equivalent,"
                          " not subscription spend."}
    long_input = _t3_cost(entry, "input_cost_per_token_above_200k_tokens")
    long_output = _t3_cost(entry, "output_cost_per_token_above_200k_tokens")
    if long_input is not None and long_output is not None:
        long_rates = {"input_tokens": long_input,
                      "output_tokens": long_output}
        long_reasoning = _t3_cost(
            entry, "output_cost_per_reasoning_token_above_200k_tokens")
        long_rates["reasoning_output_tokens"] = \
            long_reasoning if long_reasoning is not None else long_output
        long_read = _t3_cost(
            entry, "cache_read_input_token_cost_above_200k_tokens")
        if long_read is not None:
            long_rates["cached_input_tokens"] = long_read
        long_write = _t3_cost(
            entry, "cache_creation_input_token_cost_above_200k_tokens")
        if long_write is not None:
            long_rates["cache_write_input_tokens"] = long_write
        converted["long_context_threshold"] = 200000
        converted["long_context_rates"] = long_rates
    return converted


def load_t3_schedule(path: str | None = None,
                     model_semantics: dict | None = None) -> dict | None:
    """An observer schedule converted from T3's LiteLLM rate table.

    Returns None when the table is absent or unusable, so callers fall
    back to the bundled schedule. ``model_semantics`` maps observer
    model ids to the counter semantics observed for them; only listed
    models convert, and converted entries serve exactly those semantics.
    """
    target = path or t3_rates_path()
    try:
        document, fetched_ms = _read_t3_document(target)
    except (OSError, ValueError):
        return None
    if not model_semantics:
        return None
    try:
        as_of = datetime.fromtimestamp(
            fetched_ms / 1000.0, tz=timezone.utc).date().isoformat() \
            if isinstance(fetched_ms, (int, float)) and math.isfinite(
                fetched_ms) else None
    except (OverflowError, OSError, ValueError):
        as_of = None
    if as_of is None:
        try:
            as_of = date.fromtimestamp(
                os.stat(target).st_mtime).isoformat()
        except OSError:
            return None
    models = {}
    for model, semantics in sorted(model_semantics.items()):
        if not semantics:
            continue
        for key in _t3_candidates(document, model):
            converted = _t3_convert(model, set(semantics), key,
                                    document[key])
            if converted is not None:
                models[model] = converted
                break
    schedule = {"source_url": "file://" + os.path.abspath(target),
                "as_of": as_of,
                "currency": "USD",
                "unit": "USD per million tokens",
                "basis": "T3 LiteLLM rate table when present; bundled"
                         " schedule covers the rest.",
                "models": models}
    try:
        return validate_schedule(schedule)
    except ValueError:
        return None


def default_schedule(con=None) -> dict:
    """The schedule task reports price from: T3 rates over bundled fallback.

    Converted T3 entries win per model; every other bundled entry stays
    as the offline fallback. The winning source stays labeled on
    ``source_url`` (T3 file) and ``fallback_source_url`` (bundled file),
    and per-model rows keep their own source, so a valuation names the
    table behind every priced response.
    """
    try:
        bundled = load_schedule()
    except (OSError, ValueError):
        bundled = {"source_url": DEFAULT_SCHEDULE_PATH,
                   "models": {}}
    model_semantics: dict[str, set] = {}
    for model, entry in (bundled.get("models") or {}).items():
        model_semantics.setdefault(model, set()).update(
            entry.get("semantics") or [])
    if con is not None:
        try:
            rows = con.execute(
                "SELECT DISTINCT model, semantics FROM responses"
                " WHERE model IS NOT NULL").fetchall()
        except sqlite3.Error:
            rows = []
        for row in rows:
            try:
                model, semantics = row["model"], row["semantics"]
            except (KeyError, TypeError, IndexError):
                continue
            if isinstance(model, str) and model:
                model_semantics.setdefault(model, set())
                if isinstance(semantics, str) and semantics:
                    model_semantics[model].add(semantics)
    converted = load_t3_schedule(model_semantics=model_semantics)
    if converted is None:
        return bundled
    merged = json.loads(json.dumps(bundled))
    merged["models"].update(converted["models"])
    merged["fallback_source_url"] = bundled.get("source_url")
    merged["source_url"] = converted["source_url"]
    merged["as_of"] = converted["as_of"]
    merged["basis"] = converted["basis"]
    return validate_schedule(merged)


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
