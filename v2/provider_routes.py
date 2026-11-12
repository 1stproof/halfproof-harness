"""Compatibility alias; route authority is contracts.route."""
import sys
from contracts import route
sys.modules[__name__] = route
