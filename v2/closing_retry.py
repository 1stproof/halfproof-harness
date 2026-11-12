"""Closing refusal policy; no provider calls, scientific input or budget authority."""
from .final_checks import FAMILIES


def transient_status(status):
    return status in (402, 403, 429) or isinstance(status, int) and 500 <= status < 600


def fallback_models(policy, model, role, *, allowed, authors=()):
    """Optional policy.closing_retry.fallback_models, keyed by harness model key.

    A replacement keeps the report/verify role and reviewer model family. Writers
    also stay within the catalog's declared author models. Invalid configuration
    confers no new authority; it disables fallback, never widening a route or interrupting closing.
    """
    if role not in ('report', 'verify') or model not in FAMILIES:
        return []
    config = policy.get('closing_retry') if isinstance(policy, dict) else None
    if config is None:
        return []
    if not isinstance(config, dict):
        return []
    rows = config.get('fallback_models', {})
    if not isinstance(rows, dict):
        return []
    choices = rows.get(model, [])
    if not isinstance(choices, list) or any(not isinstance(m, str) or
            m not in allowed or m == model or FAMILIES.get(m) != FAMILIES[model] or
            role == 'report' and m not in authors for m in choices):
        return []
    return list(dict.fromkeys(choices))
