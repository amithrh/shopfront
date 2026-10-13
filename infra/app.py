#!/usr/bin/env python3
"""CDK app: stack AskOnceCore in ap-southeast-2 (the project Region; other Regions are refused).

Context (all optional):
  image_tag    ECR tag of the server image; without it only the data/secrets/ECR/logs layer is deployed.
  public_host  The Express Mode service host (from the `ServiceEndpoint` output of a previous deploy).
  min_tasks / max_tasks   Express auto-scaling bounds (default 1 / 1: legacy sessions are per process).
"""

from __future__ import annotations

import os

import aws_cdk as cdk

from askonce_infra.core_stack import PROJECT_REGION, AskOnceCoreStack

app = cdk.App()

region = os.environ.get("CDK_DEPLOY_REGION") or PROJECT_REGION
if region != PROJECT_REGION:
    raise SystemExit(f"AskOnce deploys only to {PROJECT_REGION} (the project Region), not {region}")


def context(name: str) -> str | None:
    value = app.node.try_get_context(name)
    return (str(value).strip() or None) if value is not None else None


AskOnceCoreStack(
    app,
    "AskOnceCore",
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=PROJECT_REGION),
    description="AskOnce MCP server: DynamoDB, secrets, ECR, logs and the ECS Express Mode service",
    image_tag=context("image_tag"),
    public_host=context("public_host"),
    min_tasks=int(context("min_tasks") or 1),
    max_tasks=int(context("max_tasks") or 1),
)

cdk.Tags.of(app).add("project", "askonce")
app.synth()
