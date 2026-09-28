"""Opt-in browser-observed visitor-status discovery evidence."""

from queue_load_test.status_discovery.observer import (
    BrowserNetworkObservation,
    DiscoveryDomSnapshot,
    StatusDiscoveryFactory,
    StatusDiscoveryFactoryProtocol,
)

__all__ = [
    "BrowserNetworkObservation",
    "DiscoveryDomSnapshot",
    "StatusDiscoveryFactory",
    "StatusDiscoveryFactoryProtocol",
]
