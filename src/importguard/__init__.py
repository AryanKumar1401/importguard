"""importguard - Measure and enforce import-time behavior in Python projects."""

from .core import check_import
from .models import ImportResult, ImportTiming, Violation, ViolationType

__version__ = "0.1.0"
__all__ = [
    "ImportResult",
    "ImportTiming",
    "Violation",
    "ViolationType",
    "check_import",
]
