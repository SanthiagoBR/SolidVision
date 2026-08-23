"""API v1 presentation package.

Version one of the product's HTTP surface. The prefix exists before there
are clients rather than after, because a search response is exactly the
shape that grows a thumbnail URL or a `total` once a UI needs one
(RFC-027, RFC-028), and versioning something already published is a
migration rather than a decision.

`/health` is deliberately not part of this: a health probe is
infrastructure, not a resource in the product's API, it is what an
orchestrator or a load balancer calls at a path fixed in configuration,
and a `v2` of search would not mean a `v2` of "is the database
reachable". It stays unversioned in `app/presentation/routes/`, which
takes no further resource routes (RFC-026 section 6.1).
"""

from fastapi import APIRouter

from .routers import images_router

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(images_router)

__all__ = ["api_v1_router"]
