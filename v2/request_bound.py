"""Input-token upper bound for a serialized request, and the matching Qwen compaction point.

Requests are JSON/English/LaTeX; DeepSeek, Kimi, Qwen and GLM tokenizers average
3.5-4.5 bytes per token on such text and hex/escape-heavy content stays above 2.
Two bytes per token therefore remains an upper bound on input tokens, so the cost
reservation computed from max_input_tokens still bounds the real charge, while a
64,000-token shape is no longer limited to 60 KB (live swarm check 4: research
workers failed reactive compaction at that size).
"""
# Spend-then-assess (2026-10-05): no reservation depends on this bound any more,
# so it only keeps requests near the declared input shape; 4 bytes/token is the
# typical rate on this text (2 made 64k-token shapes hold ~26k real tokens).
BYTES_PER_TOKEN_FLOOR = 4
MARGIN_TOKENS = 4096
# Swarm workers compact at this fraction of their declared input shape so a
# turn's tool output fits before the gateway bound is reached.
COMPACT_FRACTION = 0.35


def token_upper_bound(nbytes):
    return -(-int(nbytes) // BYTES_PER_TOKEN_FLOOR)


def fits(nbytes, max_input_tokens):
    return token_upper_bound(nbytes) + MARGIN_TOKENS <= max_input_tokens


# turn-audit 2026-10-11: the pinned client resends every earlier turn's reasoning as
# reasoning_content/reasoning on assistant messages. Providers do not bill it: over 644
# p06 turns, billed prompt tokens rose 0.33-0.35 per visible byte and -0.03..0.07 per
# reasoning byte (all four worker models). Counting it made GLM and Qwen workers hit
# HTTP 413 on 17% of turns (DeepSeek 6%, Kimi 1.4%) far below the declared input shape,
# each followed by an emergency compaction that discarded the session's reading.
# The size check therefore measures the request without those unbilled fields.
UNBILLED_ASSISTANT_FIELDS = ('reasoning_content', 'reasoning', 'reasoning_details')


def fit_bytes(request):
    """Serialized size of the request as the provider bills it (past reasoning excluded)."""
    import json
    messages = request.get('messages')
    if isinstance(messages, list):
        request = dict(request, messages=[
            {k: v for k, v in m.items() if k not in UNBILLED_ASSISTANT_FIELDS}
            if isinstance(m, dict) and m.get('role') == 'assistant' else m for m in messages])
    return len(json.dumps(request).encode())


def byte_budget(max_input_tokens):
    return (max_input_tokens - MARGIN_TOKENS) * BYTES_PER_TOKEN_FLOOR


def apply_compaction(settings, provider, compact_tokens):
    """Lower (never raise) Qwen's auto-compaction point to compact_tokens."""
    if not compact_tokens:return
    window = provider['generationConfig']['contextWindowSize']
    current = settings.setdefault('context', {}).get('autoCompactThreshold', 1.0)
    settings['context']['autoCompactThreshold'] = min(current, compact_tokens / window)


# diag-1110: compaction is itself a request carrying the whole history, so the
# compaction point must leave room for the 22.4 KB Qwen wrapper, the summarizer
# prompt and one more turn of tool output inside the byte budget. Qwen estimates
# about 4.5 bytes per token on this text.
WRAPPER_BYTES = 22400
SUMMARY_PROMPT_BYTES = 4000


# rdcheck2: halving the room compacted Qwen workers at ~24k of a 64k shape, so they spent
# much of each hour summarizing. Compact at three quarters of the shape, capped so the
# compaction request (history + wrapper + summarizer prompt + one typical tool output)
# still fits the gateway byte budget.
COMPACT_SHARE = 0.75
TOOL_OUTPUT_RESERVE_BYTES = 10000


def compact_tokens(max_input_tokens):
    fit = (byte_budget(max_input_tokens) - WRAPPER_BYTES - SUMMARY_PROMPT_BYTES - TOOL_OUTPUT_RESERVE_BYTES) / 4.5
    return max(2000, int(min(COMPACT_SHARE * max_input_tokens, fit)))
