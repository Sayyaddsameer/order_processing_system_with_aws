#!/usr/bin/env bash
# This script runs inside LocalStack to provision all required AWS resources.
# It is executed once LocalStack is healthy via the ready.d hook.
#
# All configuration is driven by environment variables — no hardcoded values.

set -e

# ---------------------------------------------------------------------------
# Configuration — all sourced from environment variables
# ---------------------------------------------------------------------------
REGION="${AWS_DEFAULT_REGION:-${AWS_REGION:-us-east-1}}"
ENDPOINT="${AWS_ENDPOINT_URL:-http://localhost:4566}"
ACCOUNT_ID="${AWS_ACCOUNT_ID:-000000000000}"

DB_HOST="${DB_HOST:-postgres}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-orders_db}"
DB_USER="${DB_USER:-orders_user}"
DB_PASSWORD="${DB_PASSWORD:-orders_password}"

SQS_QUEUE_NAME="${SQS_QUEUE_NAME:-OrderProcessingQueue}"
SQS_DLQ_NAME="${SQS_DLQ_NAME:-OrderProcessingDLQ}"
SNS_TOPIC_NAME="${SNS_TOPIC_NAME:-OrderStatusNotifications}"

LAMBDA_RUNTIME="${LAMBDA_RUNTIME:-python3.11}"
LAMBDA_TIMEOUT="${LAMBDA_TIMEOUT:-30}"
LAMBDA_MEMORY="${LAMBDA_MEMORY:-256}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"
SUCCESS_RATE="${PROCESSING_SUCCESS_RATE:-0.85}"

SQS_DLQ_RETENTION="${SQS_DLQ_RETENTION:-1209600}"     # 14 days
SQS_QUEUE_VISIBILITY="${SQS_QUEUE_VISIBILITY:-60}"
SQS_QUEUE_RETENTION="${SQS_QUEUE_RETENTION:-86400}"    # 1 day
SQS_MAX_RECEIVE="${SQS_MAX_RECEIVE:-3}"
SQS_BATCH_SIZE="${SQS_BATCH_SIZE:-5}"

echo "==================================================================="
echo "AWS Resource Provisioning"
echo "  Region   : $REGION"
echo "  Endpoint : $ENDPOINT"
echo "  Account  : $ACCOUNT_ID"
echo "==================================================================="

# ---------------------------------------------------------------------------
# Helper: wait for a command to succeed
# ---------------------------------------------------------------------------
wait_for() {
    local description="$1"
    shift
    echo "Waiting for: $description"
    until "$@" > /dev/null 2>&1; do
        echo "  Not ready yet, retrying in 2s..."
        sleep 2
    done
    echo "  Ready: $description"
}

# ---------------------------------------------------------------------------
# SQS — Dead Letter Queue first, then main queue
# ---------------------------------------------------------------------------
echo ""
echo "Creating SQS queues..."

aws --endpoint-url="$ENDPOINT" --region="$REGION" sqs create-queue \
    --queue-name "$SQS_DLQ_NAME" \
    --attributes "{\"MessageRetentionPeriod\":\"${SQS_DLQ_RETENTION}\"}" \
    --output json

DLQ_ARN="arn:aws:sqs:${REGION}:${ACCOUNT_ID}:${SQS_DLQ_NAME}"
echo "DLQ ARN: $DLQ_ARN"

DLQ_POLICY="{\"deadLetterTargetArn\":\"${DLQ_ARN}\",\"maxReceiveCount\":\"${SQS_MAX_RECEIVE}\"}"

aws --endpoint-url="$ENDPOINT" --region="$REGION" sqs create-queue \
    --queue-name "$SQS_QUEUE_NAME" \
    --attributes "{\"VisibilityTimeout\":\"${SQS_QUEUE_VISIBILITY}\",\"MessageRetentionPeriod\":\"${SQS_QUEUE_RETENTION}\",\"RedrivePolicy\":\"$(echo $DLQ_POLICY | sed 's/"/\\"/g')\"}" \
    --output json

QUEUE_URL="${ENDPOINT}/000000000000/${SQS_QUEUE_NAME}"
QUEUE_ARN="arn:aws:sqs:${REGION}:${ACCOUNT_ID}:${SQS_QUEUE_NAME}"
echo "Queue URL : $QUEUE_URL"
echo "Queue ARN : $QUEUE_ARN"

# ---------------------------------------------------------------------------
# SNS — Notifications topic
# ---------------------------------------------------------------------------
echo ""
echo "Creating SNS topic..."

aws --endpoint-url="$ENDPOINT" --region="$REGION" sns create-topic \
    --name "$SNS_TOPIC_NAME" \
    --output json

SNS_TOPIC_ARN="arn:aws:sns:${REGION}:${ACCOUNT_ID}:${SNS_TOPIC_NAME}"
echo "SNS Topic ARN: $SNS_TOPIC_ARN"

# ---------------------------------------------------------------------------
# IAM — Lambda execution role (no-op in LocalStack but preserves parity)
# ---------------------------------------------------------------------------
aws --endpoint-url="$ENDPOINT" --region="$REGION" iam create-role \
    --role-name lambda-role \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    --output json 2>/dev/null || true

LAMBDA_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/lambda-role"

# ---------------------------------------------------------------------------
# Lambda functions — Package and register each one
# ---------------------------------------------------------------------------
echo ""
echo "Packaging Lambda functions..."

# Common environment variables shared by all Lambda functions
COMMON_ENV="AWS_REGION=${REGION},AWS_ENDPOINT_URL=${ENDPOINT},AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID:-test},AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY:-test},LOG_LEVEL=${LOG_LEVEL}"
DB_ENV="DB_HOST=${DB_HOST},DB_PORT=${DB_PORT},DB_NAME=${DB_NAME},DB_USER=${DB_USER},DB_PASSWORD=${DB_PASSWORD}"

create_or_update_lambda() {
    local function_name="$1"
    local handler="$2"
    local source_dir="$3"
    local extra_env="$4"
    local zip_file="/tmp/${function_name}.zip"

    cd "$source_dir"
    zip -r "$zip_file" . -x "*.pyc" -x "__pycache__/*" -x "tests/*" > /dev/null
    cd - > /dev/null

    local full_env="${COMMON_ENV},${extra_env}"

    echo "Creating Lambda: $function_name"

    aws --endpoint-url="$ENDPOINT" --region="$REGION" lambda create-function \
        --function-name "$function_name" \
        --runtime "$LAMBDA_RUNTIME" \
        --role "$LAMBDA_ROLE_ARN" \
        --handler "$handler" \
        --zip-file "fileb://${zip_file}" \
        --environment "Variables={${full_env}}" \
        --timeout "$LAMBDA_TIMEOUT" \
        --memory-size "$LAMBDA_MEMORY" \
        --output json 2>/dev/null || \
    aws --endpoint-url="$ENDPOINT" --region="$REGION" lambda update-function-code \
        --function-name "$function_name" \
        --zip-file "fileb://${zip_file}" \
        --output json > /dev/null

    echo "Lambda $function_name is ready."
}

# OrderCreator: needs DB + SQS
create_or_update_lambda \
    "OrderCreator" \
    "app.handler" \
    "/opt/code/src/order_creator_lambda" \
    "${DB_ENV},SQS_QUEUE_URL=${QUEUE_URL}"

# OrderProcessor: needs DB + SNS
create_or_update_lambda \
    "OrderProcessor" \
    "app.handler" \
    "/opt/code/src/order_processor_lambda" \
    "${DB_ENV},SNS_TOPIC_ARN=${SNS_TOPIC_ARN},PROCESSING_SUCCESS_RATE=${SUCCESS_RATE}"

# NotificationService: no DB, no SQS/SNS publish needed
create_or_update_lambda \
    "NotificationService" \
    "app.handler" \
    "/opt/code/src/notification_service_lambda" \
    ""

# ---------------------------------------------------------------------------
# Event Source Mapping: SQS -> OrderProcessor
# ---------------------------------------------------------------------------
echo ""
echo "Mapping SQS to OrderProcessor Lambda..."

aws --endpoint-url="$ENDPOINT" --region="$REGION" lambda create-event-source-mapping \
    --function-name OrderProcessor \
    --event-source-arn "$QUEUE_ARN" \
    --batch-size "$SQS_BATCH_SIZE" \
    --enabled \
    --output json 2>/dev/null || echo "Event source mapping may already exist, skipping."

# ---------------------------------------------------------------------------
# SNS Subscription: OrderStatusNotifications -> NotificationService Lambda
# ---------------------------------------------------------------------------
echo ""
echo "Subscribing NotificationService Lambda to SNS topic..."

NOTIFICATION_LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:NotificationService"

aws --endpoint-url="$ENDPOINT" --region="$REGION" sns subscribe \
    --topic-arn "$SNS_TOPIC_ARN" \
    --protocol lambda \
    --notification-endpoint "$NOTIFICATION_LAMBDA_ARN" \
    --output json

aws --endpoint-url="$ENDPOINT" --region="$REGION" lambda add-permission \
    --function-name NotificationService \
    --statement-id sns-invoke \
    --action lambda:InvokeFunction \
    --principal sns.amazonaws.com \
    --source-arn "$SNS_TOPIC_ARN" \
    --output json 2>/dev/null || echo "Permission may already exist, skipping."

# ---------------------------------------------------------------------------
# API Gateway: REST API -> POST /orders + GET /orders/{order_id}
# ---------------------------------------------------------------------------
echo ""
echo "Setting up API Gateway..."

CREATOR_LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:OrderCreator"
STAGE_NAME="${API_STAGE_NAME:-local}"

API_ID=$(aws --endpoint-url="$ENDPOINT" --region="$REGION" apigateway create-rest-api \
    --name "OrdersAPI" \
    --description "Order processing REST API" \
    --output json | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

echo "Created API Gateway with ID: $API_ID"

ROOT_RESOURCE_ID=$(aws --endpoint-url="$ENDPOINT" --region="$REGION" apigateway get-resources \
    --rest-api-id "$API_ID" \
    --output json | python3 -c "import sys,json; items=json.load(sys.stdin)['items']; print([i['id'] for i in items if i['path']=='/'][0])")

# /orders resource
ORDERS_RESOURCE_ID=$(aws --endpoint-url="$ENDPOINT" --region="$REGION" apigateway create-resource \
    --rest-api-id "$API_ID" \
    --parent-id "$ROOT_RESOURCE_ID" \
    --path-part "orders" \
    --output json | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "Created /orders resource: $ORDERS_RESOURCE_ID"

# /orders/{order_id} resource
ORDER_ID_RESOURCE_ID=$(aws --endpoint-url="$ENDPOINT" --region="$REGION" apigateway create-resource \
    --rest-api-id "$API_ID" \
    --parent-id "$ORDERS_RESOURCE_ID" \
    --path-part "{order_id}" \
    --output json | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "Created /orders/{order_id} resource: $ORDER_ID_RESOURCE_ID"

# POST method on /orders
aws --endpoint-url="$ENDPOINT" --region="$REGION" apigateway put-method \
    --rest-api-id "$API_ID" \
    --resource-id "$ORDERS_RESOURCE_ID" \
    --http-method POST \
    --authorization-type NONE \
    --output json > /dev/null

# GET method on /orders/{order_id}
aws --endpoint-url="$ENDPOINT" --region="$REGION" apigateway put-method \
    --rest-api-id "$API_ID" \
    --resource-id "$ORDER_ID_RESOURCE_ID" \
    --http-method GET \
    --authorization-type NONE \
    --output json > /dev/null

LAMBDA_INTEGRATION_URI="arn:aws:apigateway:${REGION}:lambda:path/2015-03-31/functions/${CREATOR_LAMBDA_ARN}/invocations"

# Integrate POST /orders -> OrderCreator
aws --endpoint-url="$ENDPOINT" --region="$REGION" apigateway put-integration \
    --rest-api-id "$API_ID" \
    --resource-id "$ORDERS_RESOURCE_ID" \
    --http-method POST \
    --type AWS_PROXY \
    --integration-http-method POST \
    --uri "$LAMBDA_INTEGRATION_URI" \
    --output json > /dev/null

# Integrate GET /orders/{order_id} -> OrderCreator (status lookup)
aws --endpoint-url="$ENDPOINT" --region="$REGION" apigateway put-integration \
    --rest-api-id "$API_ID" \
    --resource-id "$ORDER_ID_RESOURCE_ID" \
    --http-method GET \
    --type AWS_PROXY \
    --integration-http-method POST \
    --uri "$LAMBDA_INTEGRATION_URI" \
    --output json > /dev/null

# Deploy API to stage
aws --endpoint-url="$ENDPOINT" --region="$REGION" apigateway create-deployment \
    --rest-api-id "$API_ID" \
    --stage-name "$STAGE_NAME" \
    --output json > /dev/null

echo "API deployed to stage: $STAGE_NAME"
echo "Endpoint: ${ENDPOINT}/restapis/${API_ID}/${STAGE_NAME}/_user_request_/orders"

# Grant API Gateway permission to invoke OrderCreator Lambda
aws --endpoint-url="$ENDPOINT" --region="$REGION" lambda add-permission \
    --function-name OrderCreator \
    --statement-id apigateway-invoke \
    --action lambda:InvokeFunction \
    --principal apigateway.amazonaws.com \
    --output json 2>/dev/null || echo "Permission may already exist, skipping."

# Persist API ID for downstream consumers (tests, etc.)
echo "$API_ID" > /tmp/api_gateway_id.txt
echo "export API_GATEWAY_ID=$API_ID" >> /etc/environment
echo "export API_GATEWAY_URL=${ENDPOINT}/restapis/${API_ID}/${STAGE_NAME}/_user_request_/orders" >> /etc/environment

echo ""
echo "==================================================================="
echo "AWS resource provisioning complete."
echo "  API Gateway ID : $API_ID"
echo "  SQS Queue URL  : $QUEUE_URL"
echo "  SNS Topic ARN  : $SNS_TOPIC_ARN"
echo "==================================================================="
