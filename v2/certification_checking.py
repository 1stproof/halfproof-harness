"""Neutral review wording; configuration contains no audit content or labels."""
from .claim_ledger import load_policy

TIGHT = ('Check every assertion and every step independently, including subsidiary assertions, '
         'examples and proof steps. For each cited result, check its exact statement and every '
         'hypothesis used in the application. A true headline does not justify a false assertion '
         'elsewhere in the submitted package. Reject on any false assertion; quote the exact false '
         'line and explain why it is false. If a step or hypothesis cannot be verified, post an '
         'objection naming that exact line and the missing verification. Judge the submitted '
         'argument, not a substitute argument. Other reviewers\' acceptances are not evidence of '
         'correctness. These instructions apply to the current claim text; superseded revisions '
         'are historical context.')

INDEPENDENT_LITERATURE = ('Prior source-review verdicts are provenance only. Independently verify every cited statement and its hypotheses from source bytes.')


def enabled():
    return load_policy('mixed-swarm').get('certification', {}).get('strict_checking', False)


def brief(text):
    return text + '\n' + TIGHT if enabled() else text
