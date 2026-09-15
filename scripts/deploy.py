"""Trigger and watch a deploy of the Render service.

    docker compose run --rm deploy            # deploy and wait
    docker compose run --rm deploy --status    # report, change nothing
    docker compose run --rm deploy --logs 50   # the service's recent output

Render deploys on push once the repository is connected, so this exists for the
cases that push does not cover: redeploying after an environment variable
changes, and recovering a service whose last deploy failed. It also reports the
service URL, which is what `provision.py --webhook-domain` and the WhatsApp
Sandbox Inbound URL both have to name.

The service itself is defined by `render.yaml` and created through Render's
Blueprints, not here. This script finds it by name and never creates one, so it
cannot bring up a second copy of a service that already exists.
"""

import argparse
import asyncio
import os
import sys

import httpx
from dotenv import load_dotenv

RENDER_API = "https://api.render.com/v1"

DEFAULT_SERVICE_NAME = "curesyngap1-agent"

# A free-instance build and rollout takes upwards of fifteen minutes, so this
# waits half an hour before giving up and telling the operator to look.
POLL_ATTEMPTS = 180
POLL_SECONDS = 10.0

# A deploy that has stopped moving, whether or not it worked.
TERMINAL_STATUSES = frozenset(
    {"live", "deactivated", "build_failed", "update_failed", "pre_deploy_failed", "canceled"}
)
SUCCESS_STATUSES = frozenset({"live"})


class DeployError(RuntimeError):
    """Something went wrong that the operator has to act on."""


async def find_service(client: httpx.AsyncClient, name: str, owner_id: str | None) -> dict:
    """The service called `name`, or raise if Render has no such service.

    Absence is an error rather than a cue to create one: the service comes from
    `render.yaml` through Blueprints, and creating it here would sidestep the
    file that is supposed to define it.
    """
    params: dict[str, object] = {"name": name, "limit": 20}
    if owner_id:
        params["ownerId"] = owner_id
    response = await client.get(f"{RENDER_API}/services", params=params)
    response.raise_for_status()

    services = [entry.get("service", entry) for entry in response.json()]
    exact = [service for service in services if service.get("name") == name]
    if not exact:
        where = f" in workspace {owner_id}" if owner_id else ""
        raise DeployError(
            f"No Render service named {name!r}{where}. Create it from render.yaml with "
            "Render's Blueprints first; see the README."
        )
    return exact[0]


async def recent_logs(
    client: httpx.AsyncClient, service_id: str, owner_id: str, limit: int
) -> list[dict]:
    """The service's most recent log lines, newest last.

    The only window onto a running deploy that this script has: an agent that
    answers without memory, or not at all, shows up here and nowhere else.
    """
    response = await client.get(
        f"{RENDER_API}/logs",
        params={"ownerId": owner_id, "resource": service_id, "limit": limit},
    )
    response.raise_for_status()
    return list(reversed(response.json().get("logs", [])))


async def latest_deploy(client: httpx.AsyncClient, service_id: str) -> dict | None:
    """The most recent deploy of the service, or None if it has never deployed."""
    response = await client.get(f"{RENDER_API}/services/{service_id}/deploys", params={"limit": 1})
    response.raise_for_status()
    deploys = [entry.get("deploy", entry) for entry in response.json()]
    return deploys[0] if deploys else None


async def trigger_deploy(client: httpx.AsyncClient, service_id: str) -> dict:
    """Start a deploy and return it.

    Render answers 202 with an empty body, so the deploy this created is read
    back from the deploys list rather than from the response.
    """
    response = await client.post(f"{RENDER_API}/services/{service_id}/deploys", json={})
    response.raise_for_status()
    if response.content:
        return response.json()

    started = await latest_deploy(client, service_id)
    if not started:
        raise DeployError(
            f"Render accepted the deploy of {service_id} with "
            f"{response.status_code} but lists no deploy for it."
        )
    return started


async def wait_for(client: httpx.AsyncClient, service_id: str, deploy_id: str) -> dict:
    """Poll until the deploy stops moving, reporting each change of status."""
    seen: str | None = None
    for _ in range(POLL_ATTEMPTS):
        response = await client.get(f"{RENDER_API}/services/{service_id}/deploys/{deploy_id}")
        response.raise_for_status()
        deploy = response.json()
        status = str(deploy.get("status") or "")

        if status != seen:
            print(f"  {status}")
            seen = status
        if status in TERMINAL_STATUSES:
            return deploy
        await asyncio.sleep(POLL_SECONDS)

    raise DeployError(
        f"Deploy {deploy_id} still {seen!r} after "
        f"{int(POLL_ATTEMPTS * POLL_SECONDS / 60)} minutes. Check the Render dashboard."
    )


def describe(service: dict) -> str:
    """The service URL, which Twilio has to be pointed at."""
    details = service.get("serviceDetails") or {}
    return str(details.get("url") or "(no URL yet)")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--service-name",
        default=os.environ.get("RENDER_SERVICE_NAME", DEFAULT_SERVICE_NAME),
        help=f"Render service to act on (default: {DEFAULT_SERVICE_NAME}).",
    )
    parser.add_argument(
        "--owner",
        default=os.environ.get("RENDER_OWNER_ID"),
        help="Render workspace id, for when the same service name exists in several.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Report the service and its last deploy without starting one.",
    )
    parser.add_argument(
        "--logs",
        type=int,
        metavar="N",
        help="Print the last N log lines and exit, starting no deploy.",
    )
    args = parser.parse_args()

    api_key = os.environ.get("RENDER_API_KEY")
    if not api_key:
        print(
            "RENDER_API_KEY is not set. Create one at Render > Account Settings > "
            "API Keys and add it to .env.",
            file=sys.stderr,
        )
        return 1

    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        service = await find_service(client, args.service_name, args.owner)
        service_id = str(service["id"])
        print(f"Service {args.service_name} ({service_id})")
        print(f"  url: {describe(service)}")
        print(f"  health: {describe(service)}/healthz")

        if args.logs:
            if not args.owner:
                raise DeployError(
                    "Reading logs needs a workspace id. Set RENDER_OWNER_ID in .env or "
                    "pass --owner."
                )
            for line in await recent_logs(client, service_id, args.owner, args.logs):
                print(f"  {line.get('timestamp', '')} {line.get('message', '')}")
            return 0

        if args.status:
            deploy = await latest_deploy(client, service_id)
            if not deploy:
                print("  no deploys yet")
            else:
                print(f"  last deploy {deploy.get('id')}: {deploy.get('status')}")
                print(f"  finished: {deploy.get('finishedAt') or 'still running'}")
            return 0

        deploy = await trigger_deploy(client, service_id)
        deploy_id = str(deploy["id"])
        print(f"Deploy {deploy_id} started")
        final = await wait_for(client, service_id, deploy_id)

        status = str(final.get("status") or "")
        if status not in SUCCESS_STATUSES:
            raise DeployError(
                f"Deploy {deploy_id} ended {status!r}. Run with --logs to see what the "
                "service said, or open the build log in the Render dashboard."
            )
        print(f"Live at {describe(service)}")
        print(
            "If the hostname changed, re-run provision.py --webhook-domain and update the "
            "WhatsApp Sandbox Inbound URL."
        )
        return 0


if __name__ == "__main__":
    load_dotenv()
    try:
        raise SystemExit(asyncio.run(main()))
    except DeployError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
    except httpx.HTTPStatusError as error:
        print(
            f"Render API returned {error.response.status_code}: {error.response.text}",
            file=sys.stderr,
        )
        raise SystemExit(1) from error
