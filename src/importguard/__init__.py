"""importguard - Measure and enforce import-time behavior in Python projects."""

from .core import check_import, run_import_subprocess
from .models import ImportResult, ImportTiming, SubprocessResult, Violation, ViolationType

__version__ = "0.1.0"
__all__ = [
    "ImportResult",
    "ImportTiming",
    "SubprocessResult",
    "Violation",
    "ViolationType",
    "check_import",
    "run_import_subprocess",
]
