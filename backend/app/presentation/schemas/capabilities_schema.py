"""What this installation can do, as a UI needs to know it (RFC-031 section 9).

Five fields since RFC-038, and the bar for a sixth is stated rather than left
to taste: *the UI draws something different because of it*. A capabilities
route that drifts into being a configuration dump is the failure RFC-031
section 15 names, and the guard against it is the criterion, not the
count.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CapabilitiesSchema(BaseModel):
    """What the client is allowed to assume about this server."""

    local_file_actions: bool = Field(
        description=(
            "Whether POST /api/v1/images/{id}/reveal exists on this "
            "installation. **Ask before drawing the button.** With the "
            "setting off that route answers 404 with the body of a route "
            "that does not exist, so discovering it by trying means a "
            "button that disappears after it is clicked (RFC-030 section 6)."
        )
    )
    platform: str = Field(
        description=(
            "The operating system this server runs on, as Python names it "
            "-- 'win32'. Only the Windows volume adapter and the Windows "
            "file revealer ship today (RFC-027 section 4.1)."
        )
    )
    version: str = Field(
        description="The application version, from settings.project_version"
    )
    max_search_limit: int = Field(
        description=(
            "The largest limit a search accepts: what 'show more' asks for. A "
            "larger one is refused with 400, never clamped (RFC-038 section 4.4)."
        )
    )
    min_radius_m: float = Field(
        description=(
            "The smallest radius, in metres, a 'near here' search accepts: the "
            "first of the radii to offer. Photo positions are where the drone "
            "was, not the ground it photographed (RFC-032 section 6)."
        )
    )
