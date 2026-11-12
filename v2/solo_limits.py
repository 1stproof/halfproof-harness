"""Bounded solo closing requests, shared by preflight and live admission.

Four full requests fit each protected half-reserve at the recorded pilot
prices, allowing evidence reads and submission without spending author money.
Research retains its original request limits. Total arm caps are unchanged.
"""
INPUT_TOKENS = 64_000
OUTPUT_TOKENS = None  # provider maximum per model (output_limits); never capped
RESERVED_REQUESTS = 1
