"""Synth-level checks of AskOnceCore against what the server's DynamoDB stores and settings expect."""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from askonce_infra.core_stack import PLACEHOLDER_HOST, AskOnceCoreStack, normalize_host

HOST = "askonce-mcp-ab12cd.ecs.ap-southeast-2.on.aws"


def synth(**kwargs: object) -> Template:
    app = cdk.App()
    stack = AskOnceCoreStack(app, "AskOnceCore", env=cdk.Environment(region="ap-southeast-2"), **kwargs)
    cdk.Tags.of(app).add("project", "askonce")
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def core() -> Template:
    return synth()


@pytest.fixture(scope="module")
def full() -> Template:
    return synth(image_tag="abc123", public_host=f"https://{HOST}/")


def container(template: Template) -> dict:
    (definition,) = template.find_resources("AWS::ECS::TaskDefinition").values()
    (main,) = definition["Properties"]["ContainerDefinitions"]
    return main


def env_of(template: Template) -> dict[str, str]:
    return {e["Name"]: e["Value"] for e in container(template)["Environment"]}


def test_main_table_matches_the_single_table_design(core: Template) -> None:
    core.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "TableName": "askonce",
            "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            "BillingMode": "PAY_PER_REQUEST",
            "TimeToLiveSpecification": {"AttributeName": "expires_at", "Enabled": True},
            "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": False},
            "GlobalSecondaryIndexes": [
                {
                    "IndexName": f"gsi{n}",
                    "KeySchema": [
                        {"AttributeName": f"gsi{n}pk", "KeyType": "HASH"},
                        {"AttributeName": f"gsi{n}sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
                for n in (1, 2, 3)
            ],
        },
    )


def test_tasks_table_matches_mcp_tasks_table_definition(core: Template) -> None:
    from_package = {
        "TableName": "askonce-tasks",
        "KeySchema": [{"AttributeName": "taskId", "KeyType": "HASH"}],
        "BillingMode": "PAY_PER_REQUEST",
        "TimeToLiveSpecification": {"AttributeName": "expiresAt", "Enabled": True},
        "GlobalSecondaryIndexes": [
            {
                "IndexName": "byPrincipal",
                "KeySchema": [
                    {"AttributeName": "principal", "KeyType": "HASH"},
                    {"AttributeName": "createdAt", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    }
    core.has_resource_properties("AWS::DynamoDB::Table", from_package)


def test_core_layer_has_no_service_until_an_image_exists(core: Template) -> None:
    core.resource_count_is("AWS::ECS::ExpressGatewayService", 0)
    core.resource_count_is("AWS::ECS::TaskDefinition", 0)
    core.resource_count_is("AWS::SecretsManager::Secret", 4)
    core.has_resource_properties("AWS::Logs::LogGroup", {"RetentionInDays": 7})
    core.has_resource_properties("AWS::ECR::Repository", {"RepositoryName": "askonce-mcp"})


def test_secrets_are_generated_never_literal(core: Template) -> None:
    for secret in core.find_resources("AWS::SecretsManager::Secret").values():
        props = secret["Properties"]
        assert "SecretString" not in props
        assert props["GenerateSecretString"]["PasswordLength"] >= 32


def test_service_runs_arm64_main_container_on_8000_with_health(full: Template) -> None:
    full.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {"RuntimePlatform": {"CpuArchitecture": "ARM64", "OperatingSystemFamily": "LINUX"}},
    )
    main = container(full)
    assert main["Name"] == "Main"
    assert main["PortMappings"] == [{"ContainerPort": 8000, "Name": "http", "Protocol": "tcp"}]
    full.has_resource_properties(
        "AWS::ECS::ExpressGatewayService",
        {"HealthCheckPath": "/health", "ServiceName": "askonce-mcp", "TaskDefinitionArn": Match.any_value()},
    )


def test_service_env_is_prod_dynamodb_and_names_the_public_host(full: Template) -> None:
    env = env_of(full)
    assert env["ASKONCE_ENV"] == "prod"
    assert env["ASKONCE_STORAGE"] == "dynamodb"
    assert env["ASKONCE_ALLOWED_HOSTS"] == HOST
    assert env["ASKONCE_ALLOWED_ORIGINS"] == env["ASKONCE_CORS_ORIGINS"] == f"https://{HOST}"
    assert env["ASKONCE_PUBLIC_BASE_URL"] == f"https://{HOST}"
    secrets = {s["Name"] for s in container(full)["Secrets"]}
    assert secrets == {
        "ASKONCE_REQUEST_STATE_KEYS",
        "ASKONCE_OWNER_LINK_KEYS",
        "ASKONCE_ID_KEY",
        "ASKONCE_ADMIN_TOKEN",
    }
    assert not any("KEY" in name or "TOKEN" in name for name in env)


def test_placeholder_host_without_public_host() -> None:
    assert env_of(synth(image_tag="abc123"))["ASKONCE_ALLOWED_HOSTS"] == PLACEHOLDER_HOST


def test_task_role_is_dynamodb_on_the_two_tables_only(full: Template) -> None:
    roles = full.find_resources("AWS::IAM::Role", {"Properties": {"Description": Match.string_like_regexp("DynamoDB")}})
    (role_id,) = roles
    policies = full.find_resources("AWS::IAM::Policy", {"Properties": {"Roles": [{"Ref": role_id}]}})
    (policy,) = policies.values()
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    text = json.dumps(statements)
    assert '"*"' not in text
    for statement in statements:
        actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        assert all(a.startswith("dynamodb:") for a in actions)
        assert not {"dynamodb:Scan", "dynamodb:DeleteTable", "dynamodb:*"} & set(actions)


def test_everything_taggable_is_tagged_project_askonce(full: Template) -> None:
    for kind in ("AWS::DynamoDB::Table", "AWS::ECR::Repository", "AWS::ECS::ExpressGatewayService", "AWS::IAM::Role"):
        for resource in full.find_resources(kind).values():
            assert {"Key": "project", "Value": "askonce"} in resource["Properties"]["Tags"], kind


@pytest.mark.parametrize(
    ("raw", "host"),
    [(f"https://{HOST}/", HOST), (HOST, HOST), (f"{HOST.upper()}", HOST), (f"https://{HOST}/mcp", HOST)],
)
def test_normalize_host(raw: str, host: str) -> None:
    assert normalize_host(raw) == host


@pytest.mark.parametrize("raw", ["", "host:443", "*.on.aws"])
def test_normalize_host_rejects(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_host(raw)
