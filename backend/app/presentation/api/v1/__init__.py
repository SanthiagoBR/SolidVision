"""API v1 presentation package.

Version one of the product's HTTP surface. The prefix exists before there
are clients rather than after, because a search response is exactly the
shape that grows a thumbnail URL or a `total` once a UI needs one
(RFC-027, RFC-028), and versioning something already published is a
migration rather than a decision.

`/capabilities` (RFC-031 section 9) *is* part of it, and the difference
from `/health` is worth stating because the two look alike. A health
probe says whether the process is up, to a monitor that is not a person.
`/capabilities` says what this installation supports, to the UI, so it
knows which buttons exist -- it is a resource of the product's API, it
would plausibly change shape in a `v2`, and it is behind the loopback
guard precisely because it is nobody else's business.

`/health` is deliberately not part of this: a health probe is
infrastructure, not a resource in the product's API, it is what an
orchestrator or a load balancer calls at a path fixed in configuration,
and a `v2` of search would not mean a `v2` of "is the database
reachable". It stays unversioned in `app/presentation/routes/`, which
takes no further resource routes (RFC-026 section 6.1).
"""

from fastapi import APIRouter

from .routers import (
    capabilities_router,
    devices_router,
    images_router,
    jobs_router,
)

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(images_router)
api_v1_router.include_router(jobs_router)
api_v1_router.include_router(devices_router)
api_v1_router.include_router(capabilities_router)

__all__ = ["api_v1_router"]
