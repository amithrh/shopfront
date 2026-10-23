# Deployment record

Deployed **2026-10-23 10:10 IST** for the demo recording, to be **torn down after recording** (the user's choice: no
running cost after the video). Image `askonce-mcp:7b458c60fca8`, AWS project 393764322224, Region ap-southeast-2.

- MCP endpoint: `https://as-881bf7de362344fabe0f1e76d2f4dffc.ecs.ap-southeast-2.on.aws/mcp` (protocol 2026-07-28, plus the 2025-11-25 fallback)
- Health: `https://as-881bf7de362344fabe0f1e76d2f4dffc.ecs.ap-southeast-2.on.aws/health` → `{"status":"ok","storage":"dynamodb"}`
- Smoke test from India: 16 tools in both protocol eras; `find_business` 865 ms, `find_item` 389 ms,
  `check_service_area` 543 ms. The three CERTIFIED demo profiles were seeded with `scripts/seed_dynamodb.py`.

## Resources (stack AskOnceCore)
```
AWS::SecretsManager::Secret  arn:aws:secretsmanager:ap-southeast-2:393764322224:secret:AdminToken38B307E8-kt6TKb61qiA2-EJiCAA
AWS::CDK::Metadata  120b36a0-cddc-11f1-b939-0affc148ce03
AWS::ECS::Cluster  askonce
AWS::IAM::Role  AskOnceCore-ExpressInfrastructureRoleBE2293B0-reUaKv5IGaqa
AWS::SecretsManager::Secret  arn:aws:secretsmanager:ap-southeast-2:393764322224:secret:IdKeyF19D5D60-JbbnnrCJP9GZ-iiZD7Q
AWS::DynamoDB::Table  askonce
AWS::SecretsManager::Secret  arn:aws:secretsmanager:ap-southeast-2:393764322224:secret:OwnerLinkKey9EA58B6E-XBc0qGmchFx9-V4JGgb
AWS::ECR::Repository  askonce-mcp
AWS::SecretsManager::Secret  arn:aws:secretsmanager:ap-southeast-2:393764322224:secret:RequestStateKeyB934ABD1-thVKHIcF4Bj5-AOr0p5
AWS::ECS::ExpressGatewayService  arn:aws:ecs:ap-southeast-2:393764322224:service/askonce/askonce-live
AWS::Logs::LogGroup  /askonce/mcp
AWS::ECS::TaskDefinition  arn:aws:ecs:ap-southeast-2:393764322224:task-definition/askonce-live:2
AWS::IAM::Role  AskOnceCore-TaskDefinitionExecutionRole8D61C2FB-uw3dwPfNr4JY
AWS::IAM::Policy  AskOn-TaskD-pNlwYa0B076b
AWS::IAM::Role  AskOnceCore-TaskRole30FC0FBB-2uOT4W72Fk7w
AWS::IAM::Policy  AskOn-TaskR-LyJlGBqd4j50
AWS::DynamoDB::Table  askonce-tasks
```
Plus what ECS Express Mode manages on its own: a shared HTTPS load balancer, target group and certificate.
The CDK bootstrap stack `CDKToolkit` (empty bucket and registry, /bin/zsh while empty) stays.

## Approximate cost while running
About $1–1.20 a day (Fargate arm64 0.5 vCPU / 1 GB, Express gateway load balancer, 4 secrets), from Free-plan credits.

## Teardown (back to $0)
```bash
AWS_PROFILE=shopfront aws cloudformation delete-stack --stack-name AskOnceCore --region ap-southeast-2
AWS_PROFILE=shopfront aws cloudformation wait stack-delete-complete --stack-name AskOnceCore --region ap-southeast-2
```
Every resource in the stack uses RemovalPolicy.DESTROY, and the image registry empties itself first.
