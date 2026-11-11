"""RFC-050 section 10: what GitHub says of the test PR's merge.

    python experiments/rfc-050-suite-do-backend/read_pr.py <number>   # needs GITHUB_TOKEN

The PR comes from a measurement branch with a red test planted, opened
against the default branch, and closed without merge. Reads, at once:

- by GraphQL, `mergeStateStatus`, `viewerCanMergeAsAdmin`, and each check of
  the head commit with its conclusion, its event and `isRequired`, which is
  the ruleset's answer for that PR;
- by REST, `mergeable_state`, the field section 10 named.

Adds the reading to read_pr.json beside this file (a list: the REST field
read right after the checks finished said `unstable`, and `blocked` minutes
later, so each reading keeps its time).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

OUT = Path(__file__).with_name("read_pr.json")
HEADERS = {
    "Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN', '')}",
    "User-Agent": "solidvision-rfc-050-measure",
    "Accept": "application/vnd.github+json",
}
QUERY = """
query($number: Int!) {
  repository(owner: "SanthiagoBR", name: "SolidVision") {
    pullRequest(number: $number) {
      state mergeable mergeStateStatus viewerCanMergeAsAdmin baseRefName
      commits(last: 1) { nodes { commit { oid statusCheckRollup { state
        contexts(first: 20) { nodes {
          ... on CheckRun { name conclusion isRequired(pullRequestNumber: $number)
                            checkSuite { workflowRun { event } app { databaseId } } }
        } } } } } }
    }
  }
}
"""


def call(url: str, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=HEADERS)
    return json.loads(urllib.request.urlopen(request, timeout=60).read())


def main() -> None:
    number = int(sys.argv[1])
    graphql = call(
        "https://api.github.com/graphql",
        {"query": QUERY, "variables": {"number": number}},
    )["data"]["repository"]["pullRequest"]
    rest = call(f"https://api.github.com/repos/SanthiagoBR/SolidVision/pulls/{number}")
    commit = graphql["commits"]["nodes"][0]["commit"]
    reading = {
        "read_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pr": number,
        "state": rest["state"],
        "merged": rest["merged"],
        "rest_mergeable_state": rest["mergeable_state"],
        "graphql_merge_state_status": graphql["mergeStateStatus"],
        "viewer_can_merge_as_admin": graphql["viewerCanMergeAsAdmin"],
        "base": graphql["baseRefName"],
        "head": commit["oid"][:7],
        "checks": [
            {
                "name": check["name"],
                "conclusion": check["conclusion"],
                "event": check["checkSuite"]["workflowRun"]["event"],
                "app": check["checkSuite"]["app"]["databaseId"],
                "required": check["isRequired"],
            }
            for check in commit["statusCheckRollup"]["contexts"]["nodes"]
        ],
    }
    readings = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else []
    readings.append(reading)
    OUT.write_text(json.dumps(readings, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(reading, indent=2))


if __name__ == "__main__":
    main()
