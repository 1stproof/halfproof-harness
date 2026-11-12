"""Per-reply output is never capped below the provider's maximum (operator ruling 2026-10-05).

Values are each endpoint's max_completion_tokens from the live route preflight of
2026-10-05. Money budgets and wall clock are the only limits; every reservation
is sized against these maxima, so a reply of any length stays inside its budget.
"""
PROVIDER_MAX_COMPLETION_TOKENS = {
    'deepseek/deepseek-v4.1-flash': 943718,
    'deepseek/deepseek-v4-pro': 943718,
    'moonshotai/kimi-k3': 943718,
    'z-ai/glm-5.3': 131072,
    'qwen/qwen3.8-2.4t-a95b': 131072,
}
LARGEST = max(PROVIDER_MAX_COMPLETION_TOKENS.values())


def provider_max(model_id):
    """Synthetic fixture models (tests) fall back to the largest maximum."""
    return PROVIDER_MAX_COMPLETION_TOKENS.get(model_id, LARGEST)
