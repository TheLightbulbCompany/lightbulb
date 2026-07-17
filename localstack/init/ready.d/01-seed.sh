#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# 01-seed.sh — Seed LocalStack with the AWS resources the Isol8 backend needs.
#              Runs inside the LocalStack container via /etc/localstack/init/ready.d/
# ---------------------------------------------------------------------------

log() { echo "[isol8-seed] $*"; }

# Eval a check command; return 0 if it succeeds, 1 otherwise.
resource_exists() {
  eval "$1" >/dev/null 2>&1
}

# ── 1. RDS PostgreSQL ────────────────────────────────────────────────────────
log "1/12  RDS PostgreSQL instance"
if resource_exists "awslocal rds describe-db-instances --db-instance-identifier isol8-local-db"; then
  log "       ↳ already exists, skipping"
else
  awslocal rds create-db-instance \
    --db-instance-identifier isol8-local-db \
    --db-instance-class db.t3.micro \
    --engine postgres \
    --master-username postgres \
    --master-user-password postgres \
    --db-name securechat \
    --allocated-storage 20
  log "       ↳ created"
fi

# ── 2. DynamoDB table ────────────────────────────────────────────────────────
log "2/12  DynamoDB table"
if resource_exists "awslocal dynamodb describe-table --table-name isol8-local-websocket-connections"; then
  log "       ↳ already exists, skipping"
else
  awslocal dynamodb create-table \
    --table-name isol8-local-websocket-connections \
    --attribute-definitions AttributeName=connectionId,AttributeType=S \
    --key-schema AttributeName=connectionId,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST
  log "       ↳ created"
fi

# ── 3. S3 bucket ─────────────────────────────────────────────────────────────
log "3/12  S3 bucket"
if resource_exists "awslocal s3api head-bucket --bucket isol8-local-openclaw-configs"; then
  log "       ↳ already exists, skipping"
else
  awslocal s3 mb s3://isol8-local-openclaw-configs
  log "       ↳ created"
fi

# ── 4. EFS file system ───────────────────────────────────────────────────────
log "4/12  EFS file system"
EFS_ID=$(awslocal efs describe-file-systems \
  --creation-token isol8-local-efs \
  --query 'FileSystems[0].FileSystemId' --output text 2>/dev/null || true)

if [ -n "$EFS_ID" ] && [ "$EFS_ID" != "None" ]; then
  log "       ↳ already exists ($EFS_ID), skipping"
else
  EFS_ID=$(awslocal efs create-file-system \
    --creation-token isol8-local-efs \
    --query 'FileSystemId' --output text)
  log "       ↳ created ($EFS_ID)"
fi

# ── 5. ECS cluster ───────────────────────────────────────────────────────────
log "5/12  ECS cluster"
if resource_exists "awslocal ecs describe-clusters --clusters isol8-local --query 'clusters[?status==\`ACTIVE\`].clusterName' --output text | grep -q isol8-local"; then
  log "       ↳ already exists, skipping"
else
  awslocal ecs create-cluster --cluster-name isol8-local
  log "       ↳ created"
fi

# ── 6. Cloud Map ─────────────────────────────────────────────────────────────
log "6/12  Cloud Map namespace + service"

NAMESPACE_ID=$(awslocal servicediscovery list-namespaces \
  --query "Namespaces[?Name=='isol8.local'].Id" --output text 2>/dev/null || true)

if [ -n "$NAMESPACE_ID" ] && [ "$NAMESPACE_ID" != "None" ]; then
  log "       ↳ namespace already exists ($NAMESPACE_ID)"
else
  OPERATION_ID=$(awslocal servicediscovery create-private-dns-namespace \
    --name isol8.local \
    --vpc vpc-local \
    --query 'OperationId' --output text)
  log "       ↳ namespace creation started (operation $OPERATION_ID)"

  # Poll until the operation completes (no sleep — tight poll loop).
  for _i in $(seq 1 30); do
    OP_STATUS=$(awslocal servicediscovery get-operation \
      --operation-id "$OPERATION_ID" \
      --query 'Operation.Status' --output text 2>/dev/null || echo "PENDING")
    if [ "$OP_STATUS" = "SUCCESS" ]; then
      break
    fi
    if [ "$OP_STATUS" = "FAIL" ]; then
      log "       ↳ ERROR: namespace creation failed"
      exit 1
    fi
  done

  NAMESPACE_ID=$(awslocal servicediscovery list-namespaces \
    --query "Namespaces[?Name=='isol8.local'].Id" --output text)
  log "       ↳ namespace ready ($NAMESPACE_ID)"
fi

# Cloud Map service
CLOUDMAP_SERVICE_ID=$(awslocal servicediscovery list-services \
  --query "Services[?Name=='openclaw' && NamespaceId=='${NAMESPACE_ID}'].Id" \
  --output text 2>/dev/null || true)

if [ -n "$CLOUDMAP_SERVICE_ID" ] && [ "$CLOUDMAP_SERVICE_ID" != "None" ]; then
  log "       ↳ service already exists ($CLOUDMAP_SERVICE_ID)"
else
  CLOUDMAP_SERVICE_ID=$(awslocal servicediscovery create-service \
    --name openclaw \
    --namespace-id "$NAMESPACE_ID" \
    --dns-config "NamespaceId=${NAMESPACE_ID},DnsRecords=[{Type=A,TTL=10}]" \
    --query 'Service.Id' --output text)
  log "       ↳ service created ($CLOUDMAP_SERVICE_ID)"
fi

CLOUDMAP_SERVICE_ARN=$(awslocal servicediscovery get-service \
  --id "$CLOUDMAP_SERVICE_ID" \
  --query 'Service.Arn' --output text)

# ── 7. Secrets Manager ───────────────────────────────────────────────────────
log "7/12  Secrets Manager"

# Generate a Fernet key for encryption_key (base64-encoded 32-byte key).
FERNET_KEY=$(python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())")

declare -A SECRETS=(
  ["isol8/local/clerk_issuer"]="${CLERK_ISSUER:-https://clerk.isol8.local}"
  ["isol8/local/clerk_secret_key"]="${CLERK_SECRET_KEY:-sk_test_placeholder}"
  ["isol8/local/stripe_secret_key"]="${STRIPE_SECRET_KEY:-sk_test_placeholder}"
  ["isol8/local/stripe_webhook_secret"]="${STRIPE_WEBHOOK_SECRET:-whsec_placeholder}"
  ["isol8/local/encryption_key"]="${ENCRYPTION_KEY:-$FERNET_KEY}"
  ["isol8/local/database_url"]="${DATABASE_URL:-postgresql+asyncpg://postgres:postgres@db:5432/securechat}"
)

for secret_name in "${!SECRETS[@]}"; do
  if resource_exists "awslocal secretsmanager describe-secret --secret-id $secret_name"; then
    log "       ↳ $secret_name already exists, skipping"
  else
    awslocal secretsmanager create-secret \
      --name "$secret_name" \
      --secret-string "${SECRETS[$secret_name]}"
    log "       ↳ $secret_name created"
  fi
done

# Resolve the encryption key we actually stored.
ENCRYPTION_KEY_VALUE="${ENCRYPTION_KEY:-$FERNET_KEY}"

# ── 8. KMS key ───────────────────────────────────────────────────────────────
log "8/12  KMS key"
KMS_KEY_ID=$(awslocal kms list-aliases \
  --query "Aliases[?AliasName=='alias/isol8-local'].TargetKeyId" \
  --output text 2>/dev/null || true)

if [ -n "$KMS_KEY_ID" ] && [ "$KMS_KEY_ID" != "None" ]; then
  log "       ↳ already exists ($KMS_KEY_ID), skipping"
else
  KMS_KEY_ID=$(awslocal kms create-key \
    --description "isol8-local" \
    --query 'KeyMetadata.KeyId' --output text)
  awslocal kms create-alias \
    --alias-name alias/isol8-local \
    --target-key-id "$KMS_KEY_ID"
  log "       ↳ created ($KMS_KEY_ID)"
fi

# ── 9. ECS Task Definition ───────────────────────────────────────────────────
log "9/12  ECS task definition"
if resource_exists "awslocal ecs describe-task-definition --task-definition isol8-local-openclaw"; then
  log "       ↳ already exists, skipping"
else
  awslocal ecs register-task-definition \
    --family isol8-local-openclaw \
    --requires-compatibilities FARGATE \
    --network-mode awsvpc \
    --cpu "512" \
    --memory "1024" \
    --container-definitions "[
      {
        \"name\": \"openclaw\",
        \"image\": \"ghcr.io/openclaw/openclaw:latest\",
        \"portMappings\": [{\"containerPort\": 18789, \"protocol\": \"tcp\"}],
        \"mountPoints\": [{\"sourceVolume\": \"openclaw-efs\", \"containerPath\": \"/home/openclaw/.openclaw\"}],
        \"essential\": true
      }
    ]" \
    --volumes "[
      {
        \"name\": \"openclaw-efs\",
        \"efsVolumeConfiguration\": {
          \"fileSystemId\": \"${EFS_ID}\",
          \"rootDirectory\": \"/\"
        }
      }
    ]"
  log "       ↳ registered"
fi

# ── 10. WebSocket API Gateway V2 ─────────────────────────────────────────────
log "10/12 WebSocket API Gateway"

# 10a. Lambda authorizer function
if resource_exists "awslocal lambda get-function --function-name isol8-local-ws-authorizer"; then
  log "       ↳ authorizer lambda already exists, skipping"
else
  LAMBDA_DIR=$(mktemp -d)
  cat > "${LAMBDA_DIR}/index.py" <<'PYEOF'
def handler(event, context):
    method_arn = event.get("methodArn", "*")
    return {
        "principalId": "local-user",
        "policyDocument": {"Version": "2012-10-17", "Statement": [{"Action": "execute-api:Invoke", "Effect": "Allow", "Resource": method_arn}]},
        "context": {"userId": "local-user", "orgId": ""}
    }
PYEOF
  (cd "${LAMBDA_DIR}" && zip -j function.zip index.py)

  awslocal lambda create-function \
    --function-name isol8-local-ws-authorizer \
    --runtime python3.11 \
    --handler index.handler \
    --role arn:aws:iam::000000000000:role/lambda-role \
    --zip-file "fileb://${LAMBDA_DIR}/function.zip"
  rm -rf "${LAMBDA_DIR}"
  log "       ↳ authorizer lambda created"
fi

AUTHORIZER_LAMBDA_ARN=$(awslocal lambda get-function \
  --function-name isol8-local-ws-authorizer \
  --query 'Configuration.FunctionArn' --output text)

# 10b. WebSocket API
WS_API_ID=$(awslocal apigatewayv2 get-apis \
  --query "Items[?Name=='isol8-local-ws'].ApiId" --output text 2>/dev/null || true)

if [ -n "$WS_API_ID" ] && [ "$WS_API_ID" != "None" ]; then
  log "       ↳ WebSocket API already exists ($WS_API_ID)"
else
  WS_API_ID=$(awslocal apigatewayv2 create-api \
    --name isol8-local-ws \
    --protocol-type WEBSOCKET \
    --route-selection-expression '$request.body.action' \
    --query 'ApiId' --output text)
  log "       ↳ WebSocket API created ($WS_API_ID)"
fi

# 10c. Authorizer
WS_AUTH_ID=$(awslocal apigatewayv2 get-authorizers \
  --api-id "$WS_API_ID" \
  --query "Items[?Name=='isol8-local-authorizer'].AuthorizerId" \
  --output text 2>/dev/null || true)

if [ -n "$WS_AUTH_ID" ] && [ "$WS_AUTH_ID" != "None" ]; then
  log "       ↳ authorizer already exists ($WS_AUTH_ID)"
else
  WS_AUTH_ID=$(awslocal apigatewayv2 create-authorizer \
    --api-id "$WS_API_ID" \
    --name isol8-local-authorizer \
    --authorizer-type REQUEST \
    --authorizer-uri "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/${AUTHORIZER_LAMBDA_ARN}/invocations" \
    --identity-source 'route.request.querystring.token' \
    --query 'AuthorizerId' --output text)
  log "       ↳ authorizer created ($WS_AUTH_ID)"
fi

# 10d. Integration
WS_INTEGRATION_ID=$(awslocal apigatewayv2 get-integrations \
  --api-id "$WS_API_ID" \
  --query "Items[0].IntegrationId" --output text 2>/dev/null || true)

if [ -n "$WS_INTEGRATION_ID" ] && [ "$WS_INTEGRATION_ID" != "None" ]; then
  log "       ↳ integration already exists ($WS_INTEGRATION_ID)"
else
  WS_INTEGRATION_ID=$(awslocal apigatewayv2 create-integration \
    --api-id "$WS_API_ID" \
    --integration-type HTTP_PROXY \
    --integration-uri "http://backend:8000/api/v1/ws/message" \
    --integration-method POST \
    --query 'IntegrationId' --output text)
  log "       ↳ integration created ($WS_INTEGRATION_ID)"
fi

# 10e. Routes
for route_key in '$connect' '$disconnect' '$default'; do
  EXISTING_ROUTE=$(awslocal apigatewayv2 get-routes \
    --api-id "$WS_API_ID" \
    --query "Items[?RouteKey=='${route_key}'].RouteId" \
    --output text 2>/dev/null || true)

  if [ -n "$EXISTING_ROUTE" ] && [ "$EXISTING_ROUTE" != "None" ]; then
    log "       ↳ route ${route_key} already exists"
    continue
  fi

  if [ "$route_key" = '$connect' ]; then
    awslocal apigatewayv2 create-route \
      --api-id "$WS_API_ID" \
      --route-key "$route_key" \
      --authorization-type CUSTOM \
      --authorizer-id "$WS_AUTH_ID" \
      --target "integrations/${WS_INTEGRATION_ID}"
  else
    awslocal apigatewayv2 create-route \
      --api-id "$WS_API_ID" \
      --route-key "$route_key" \
      --target "integrations/${WS_INTEGRATION_ID}"
  fi
  log "       ↳ route ${route_key} created"
done

# 10f. Deploy stage
EXISTING_STAGE=$(awslocal apigatewayv2 get-stages \
  --api-id "$WS_API_ID" \
  --query "Items[?StageName=='local'].StageName" \
  --output text 2>/dev/null || true)

if [ -n "$EXISTING_STAGE" ] && [ "$EXISTING_STAGE" != "None" ]; then
  log "       ↳ stage 'local' already exists"
else
  awslocal apigatewayv2 create-stage \
    --api-id "$WS_API_ID" \
    --stage-name local
  log "       ↳ stage 'local' deployed"
fi

WS_URL="ws://localhost:4510/${WS_API_ID}/local"

# ── 11. Pull OpenClaw Docker image ───────────────────────────────────────────
log "11/12 Pulling OpenClaw Docker image"
docker pull ghcr.io/openclaw/openclaw:latest 2>/dev/null || log "       ↳ docker pull failed (non-fatal)"

# ── 12. Write generated.env ──────────────────────────────────────────────────
log "12/12 Writing generated.env"

GENERATED_ENV="/etc/localstack/init/ready.d/generated.env"
cat > "$GENERATED_ENV" <<EOF
CLOUD_MAP_SERVICE_ARN=${CLOUDMAP_SERVICE_ARN}
EFS_FILE_SYSTEM_ID=${EFS_ID}
ENCRYPTION_KEY=${ENCRYPTION_KEY_VALUE}
NEXT_PUBLIC_WS_URL=${WS_URL}
EOF

log "       ↳ written to ${GENERATED_ENV}"

log "══════════════════════════════════════════════════"
log " LocalStack seed complete!"
log "══════════════════════════════════════════════════"
