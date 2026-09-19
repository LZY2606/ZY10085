"""diagforge: deterministic multi-file compiler diagnostic sample minimizer."""

from .model import RULES_VERSION
from .predicate import Predicate
from .runner import RunResult, run_candidate
from .store import Store
from .engine import Engine

__all__ = [
    "RULES_VERSION",
    "Predicate",
    "RunResult",
    "run_candidate",
    "Store",
    "Engine",
]

__version__ = "0.1.0"
