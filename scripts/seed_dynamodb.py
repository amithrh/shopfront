"""Publish profile JSON files into the deployed AskOnce DynamoDB table (operator tool, idempotent).

In DynamoDB storage the server never reads profile directories: profiles are published into the table
(by onboarding plus the certifier). By default this script publishes `profiles/live/`: the three demo
businesses as certified by the deterministic certifier (`scripts/certify_demo.py`).

Each file is validated with the server's own strict loader first. Then, per business:
  * not in the table          -> put (expected_revision=None)
  * same sha256 already there -> skipped
  * older revision there      -> put (compare-and-swap on the stored revision)
  * same or newer revision with different content -> refused (bump `revision` in the file to replace it)

Usage (credentials from the `shopfront` profile; Region ap-southeast-2):
  server/.venv/bin/python scripts/seed_dynamodb.py --dry-run          # validate only, no AWS calls
  AWS_PROFILE=shopfront uv run --project server --with "botocore[crt]==1.43.111" \
      python scripts/seed_dynamodb.py [--dir profiles/live]
(`aws login` profiles need botocore[crt]; see docs/friction-log.md #12.)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from askonce_server.profile.dynamodb import DynamoProfileStore
from askonce_server.profile.loader import load_profile_file

REPO = Path(__file__).resolve().parents[1]
PROJECT_REGION = "ap-southeast-2"


async def seed(args: argparse.Namespace) -> int:
    schema = REPO / "schema" / "business-profile.schema.json"
    files = sorted(Path(args.dir).glob("*.json"))
    if not files:
        print(f"no *.json files in {args.dir} (run scripts/certify_demo.py first)", file=sys.stderr)
        return 1
    profiles = [load_profile_file(path, schema_path=schema, snapshots=None, strict=True).profile for path in files]

    if args.dry_run:
        for profile in profiles:
            print(f"valid: {profile.profile_id} revision {profile.revision} live={profile.is_live} sha256={profile.sha256[:12]}")
        return 0

    import boto3

    client = boto3.session.Session().client("dynamodb", region_name=args.region)
    store = DynamoProfileStore(client, args.table, schema_path=schema, strict=True)
    refused = 0
    for profile in profiles:
        stored = await store.get(profile.profile_id)
        if stored is None:
            await store.put(profile, expected_revision=None)
            print(f"published: {profile.profile_id} revision {profile.revision}")
        elif stored.sha256 == profile.sha256:
            print(f"unchanged: {profile.profile_id} revision {stored.revision}")
        elif stored.revision < profile.revision:
            await store.put(profile, expected_revision=stored.revision)
            print(f"updated:   {profile.profile_id} revision {stored.revision} -> {profile.revision}")
        else:
            refused += 1
            print(
                f"REFUSED:   {profile.profile_id}: table holds revision {stored.revision} with other content; "
                f"bump the file's revision (now {profile.revision}) to replace it",
                file=sys.stderr,
            )
    return 1 if refused else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", default=str(REPO / "profiles" / "live"), help="directory of profile JSON files")
    parser.add_argument("--table", default="askonce", help="single table name (stack output TableName)")
    parser.add_argument("--region", default=PROJECT_REGION)
    parser.add_argument("--dry-run", action="store_true", help="validate the files only; no AWS calls")
    args = parser.parse_args()
    if args.region != PROJECT_REGION:
        parser.error(f"AskOnce runs only in {PROJECT_REGION}")
    sys.exit(asyncio.run(seed(args)))


if __name__ == "__main__":
    main()
