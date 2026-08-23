"""
ystocker.warmup
~~~~~~~~~~~~~~~
One lock that keeps cold-start cache builds from running on top of each other.

Why this exists
---------------
``create_app()`` starts nine background warm-up threads within a few
milliseconds of each other. On a warm box that is free: each one reads its disk
cache and goes back to sleep. On a *cold* box -- a fresh instance, or any module
whose cache schema just changed -- they all fall through to their download paths
simultaneously, and those are not small:

* breadth: 518 tickers x 11 years, measured at **81.9s** on the box
* housing: ~10 MB across 8 Zillow/Redfin files
* valuation: multpl scrape + constituent fundamentals
* sec13f: 22 funds of EDGAR filings
* markets warm-up + the rolling ticker refresher, both hitting Yahoo

The instance is a t3.medium: **2 vCPU**, 2 Gunicorn workers, 4 GB. Running those
concurrently starved the workers badly enough that /markets and /api/multiples
timed out for about two minutes after a restart, which is how this was found.

Serialising them does not make the total wall time worse -- these are almost all
network-bound on the same upstream, and Yahoo throttles a client that opens
everything at once anyway. It makes the box *responsive* while they run, which is
the property that actually matters, since every one of these paths already serves
stale-or-absent data quite happily in the meantime.

Contract
--------
Acquired by background threads only, never from a request path. That is what
keeps it free of lock-order inversion against each module's own build lock: a
request that cannot build cannot be holding one of those while it waits here.
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Iterator

log = logging.getLogger(__name__)

# One heavy build at a time. Not a plain Lock so the width is a one-line change
# if the box ever grows more cores than it has upstreams to saturate.
_MAX_CONCURRENT_COLD_BUILDS = 1
_gate = threading.BoundedSemaphore(_MAX_CONCURRENT_COLD_BUILDS)


@contextlib.contextmanager
def cold_build(label: str) -> Iterator[None]:
    """Serialise one expensive cold-start build against the others.

    Wrap only the *download* path, never the disk-cache read: a warm box must
    not queue nine threads behind each other to do nine cheap file reads.

    Never raises on its own account and never blocks forever -- a caller that
    waits out the timeout proceeds anyway, because a slow neighbour is not a
    reason to leave a cache cold for the rest of the process's life.
    """
    waited = 0.0
    t0 = time.time()
    # Long enough for the worst single build (breadth at ~82s) plus headroom,
    # short enough that a wedged holder cannot strand every other warm-up.
    acquired = _gate.acquire(timeout=300)
    if acquired:
        waited = time.time() - t0
        if waited > 1:
            log.info("warmup: %s waited %.0fs for the cold-build gate", label, waited)
    else:
        log.warning("warmup: %s timed out waiting for the cold-build gate "
                    "after 300s — proceeding unserialised", label)
    try:
        log.info("warmup: %s cold build starting", label)
        yield
    finally:
        built = time.time() - t0 - waited
        log.info("warmup: %s cold build finished in %.1fs", label, built)
        if acquired:
            try:
                _gate.release()
            except ValueError:  # pragma: no cover - defensive
                log.warning("warmup: %s released the gate twice", label)
