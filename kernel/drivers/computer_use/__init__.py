"""Native-first computer-use discovery and resource coordination."""

from .discovery import discover_fallbacks, discover_project, fallback_allowed, known_provider
from .lease import ResourceBusyError, ResourceLease, default_lease_root
from .policy import CAPABILITIES, Candidate, Selection, from_native, project_controls, select_provider
from .probe import probe_fallbacks

__all__ = [
    "CAPABILITIES", "Candidate", "Selection", "from_native", "project_controls", "select_provider",
    "discover_fallbacks", "discover_project", "fallback_allowed", "known_provider",
    "ResourceBusyError", "ResourceLease", "default_lease_root",
    "probe_fallbacks",
]
