"""source.kind → Adapter registry, with per-source override and generic fallback."""

from jobtrack.adapters.ashby import AshbyAdapter
from jobtrack.adapters.base import Adapter
from jobtrack.adapters.fallback import FallbackAdapter
from jobtrack.adapters.gem import GemAdapter
from jobtrack.adapters.greenhouse import GreenhouseAdapter
from jobtrack.adapters.lever import LeverAdapter
from jobtrack.adapters.rippling import RipplingAdapter
from jobtrack.adapters.smartrecruiters import SmartRecruitersAdapter
from jobtrack.adapters.workable import WorkableAdapter
from jobtrack.adapters.workday import WorkdayAdapter

_FALLBACK = FallbackAdapter()

ADAPTERS: dict[str, Adapter] = {
    "greenhouse": GreenhouseAdapter(),
    "lever": LeverAdapter(),
    "ashby": AshbyAdapter(),
    "smartrecruiters": SmartRecruitersAdapter(),
    "workable": WorkableAdapter(),
    "workday": WorkdayAdapter(),
    "gem": GemAdapter(),
    "rippling": RipplingAdapter(),
    "fallback": _FALLBACK,
}


def get_adapter(kind: str, override: str | None = None) -> Adapter:
    """Resolve the adapter for a source kind; config.adapter_override wins, and unknown
    kinds get the generic fallback adapter."""
    if override:
        return ADAPTERS.get(override, _FALLBACK)
    return ADAPTERS.get(kind, _FALLBACK)
