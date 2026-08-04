"""source.kind → Fetcher registry."""

from jobtrack.sources.ashby import AshbyFetcher
from jobtrack.sources.base import Fetcher
from jobtrack.sources.greenhouse import GreenhouseFetcher
from jobtrack.sources.lever import LeverFetcher
from jobtrack.sources.polite_http import FetchError
from jobtrack.sources.scrape.browser import BrowserFetcher
from jobtrack.sources.smartrecruiters import SmartRecruitersFetcher
from jobtrack.sources.workable import WorkableFetcher
from jobtrack.sources.workday import WorkdayFetcher

FETCHERS: dict[str, Fetcher] = {
    "greenhouse": GreenhouseFetcher(),
    "lever": LeverFetcher(),
    "ashby": AshbyFetcher(),
    "smartrecruiters": SmartRecruitersFetcher(),
    "workable": WorkableFetcher(),
    "workday": WorkdayFetcher(),
    "scrape": BrowserFetcher(),
}


def get_fetcher(kind: str) -> Fetcher:
    fetcher = FETCHERS.get(kind)
    if fetcher is None:
        raise FetchError(f"no fetcher registered for source kind {kind!r}")
    return fetcher
