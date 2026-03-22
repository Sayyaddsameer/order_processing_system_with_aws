#!/usr/bin/env bash
# This script runs inside LocalStack to provision all required AWS resources.
# It is executed once LocalStack is healthy.

set -e

REGION="${AWS_REGION:-us-east-1}"
ENDPOINT="http://localhost:4566"
ACCOUNT_ID="000000000000"

echo "Starting AWS resource provisioning..."

# --------------------------------------------------------------------------
# Helper: wait for a command to succeed
# --------------------------------------------------------------------------
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

# --------------------------------------------------------------------------
# SQS - Dead Letter Queue first, then main queue
# --------------------------------------------------------------------------
echo ""
echo "Creating SQS queues..."

aws --endpoint-url="$ENDPOINT" --region="$REGION" sqs create-queue \
    --queue-name OrderProcessingDLQ \
    --attributes '{"MessageRetentionPeriod":"1209600"}' \
    --output json

DLQ_ARN="arn:aws:sqs:${REGION}:${ACCOUNT_ID}:OrderProcessingDLQ"
echo "DLQ ARN: $DLQ_ARN"

DLQ_POLICY="{\"deadLetterTargetArn\":\"${DLQ_ARN}\",\"maxReceiveCount\":\"3\"}"

aws --endpoint-url="$ENDPOINT" --region="$REGION" sqs create-queue \
    --queue-name OrderProcessingQueue \
    --attributes "{\"VisibilityTimeout\":\"60\",\"MessageRetentionPeriod\":\"86400\",\"RedrivePolicy\":\"$(echo $DLQ_POLICY | sed 's/"/\\"/g')\"}" \
    --output json

QUEUE_URL="http://localhost:4566/000000000000/OrderProcessingQueue"
QUEUE_ARN="arn:aws:sqs:${REGION}:${ACCOUNT_ID}:OrderProcessingQueue"
echo "OrderProcessingQueue URL: $QUEUE_URL"

# --------------------------------------------------------------------------
# SNS - Notifications topic
# --------------------------------------------------------------------------
echo ""
echo "Creating SNS topic..."

aws --endpoint-url="$ENDPOINT" --region="$REGION" sns create-topic \
    --name OrderStatusNotifications \
    --output json

SNS_TOPIC_ARN="arn:aws:sns:${REGION}:${ACCOUNT_ID}:OrderStatusNotifications"
echo "SNS Topic ARN: $SNS_TOPIC_ARN"

# --------------------------------------------------------------------------
# Lambda functions - Package and register each one
# --------------------------------------------------------------------------
echo ""
echo "Packaging Lambda functions..."

# Function to zip and create/update a Lambda
create_or_update_lambda() {
    local function_name="$1"
    local handler="$2"
    local source_dir="$3"
    local zip_file="/tmp/${function_name}.zip"

    cd "$source_dir"
    zip -r "$zip_file" . -x "*.pyc" -x "__pycache__/*" -x "tests/*" > /dev/null
    cd -

    echo "Creating Lambda: $function_name"

    aws --endpoint-url="$ENDPOINT" --region="$REGION" lambda create-function \
        --function-name "$function_name" \
        --runtime python3.11 \
        --role "arn:aws:iam::${ACCOUNT_ID}:role/lambda-role" \
        --handler "$handler" \
        --zip-file "fileb://${zip_file}" \
        --environment "Variables={
            AWS_REGION=${REGION},
            AWS_ENDPOINT_URL=${ENDPOINT},
            AWS_ACCESS_KEY_ID=test,
            AWS_SECRET_ACCESS_KEY=test,
            DB_HOST=postgres,
            DB_PORT=5432,
            DB_NAME=orders_db,
            DB_USER=orders_user,
            DB_PASSWORD=orders_password,
            SQS_QUEUE_URL=http://localhost:4566/000000000000/OrderProcessingQueue,
            SNS_TOPIC_ARN=${SNS_TOPIC_ARN},
            LOG_LEVEL=INFO
        }" \
        --timeout 30 \
        --memory-size 256 \
        --output json 2>/dev/null || \
    aws --endpoint-url="$ENDPOINT" --region="$REGION" lambda update-function-code \
        --function-name "$function_name" \
        --zip-file "fileb://${zip_file}" \
        --output json > /dev/null

    echo "Lambda $function_name is ready."
}

create_or_update_lambda "OrderCreator" "app.handler" "/opt/code/src/order_creator_lambda"
create_or_update_lambda "OrderProcessor" "app.handler" "/opt/code/src/order_processor_lambda"
create_or_update_lambda "NotificationService" "app.handler" "/opt/code/src/notification_service_lambda"

# --------------------------------------------------------------------------
# Grant Lambda execution role (IAM - no-op in LocalStack but keeps parity)
# --------------------------------------------------------------------------
aws --endpoint-url="$ENDPOINT" --region="$REGION" iam create-role \
    --role-name lambda-role \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    --output json 2>/dev/null || true

# --------------------------------------------------------------------------
# Event Source Mapping: SQS -> OrderProcessor
# --------------------------------------------------------------------------
echo ""
echo "Mapping SQS to OrderProcessor Lambda..."

aws --endpoint-url="$ENDPOINT" --region="$REGION" lambda create-event-source-mapping \
    --function-name OrderProcessor \
    --event-source-arn "$QUEUE_ARN" \
    --batch-size 5 \
    --enabled \
    --output json 2>/dev/null || echo "Event source mapping may already exist, skipping."

# --------------------------------------------------------------------------
# SNS Subscription: OrderStatusNotifications -> NotificationService Lambda
# --------------------------------------------------------------------------
echo ""
echo "Subscribing NotificationService Lambda to SNS topic..."

NOTIFICATION_LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:NotificationService"

aws --endpoint-url="$ENDPOINT" --region="$REGION" sns subscribe \
    --topic-arn "$SNS_TOPIC_ARN" \
    --protocol lambda \
    --notification-endpoint "$NOTIFICATION_LAMBDA_ARN" \
    --output json

# Grant SNS permission to invoke NotificationService Lambda
aws --endpoint-url="$ENDPOINT" --region="$REGION" lambda add-permission \
    --function-name NotificationService \
    --statement-id sns-invoke \
    --action lambda:InvokeFunction \
    --principal sns.amazonaws.com \
    --source-arn "$SNS_TOPIC_ARN" \
    --output json 2>/dev/null || echo "Permission may already exist, skipping."

# --------------------------------------------------------------------------
# API Gateway: REST API -> POST /orders -> OrderCreator Lambda
# --------------------------------------------------------------------------
echo ""
echo "Setting up API Gateway..."

API_ID=$(aws --endpoint-url="$ENDPOINT" --region="$REGION" apigateway create-rest-api \
    --name "OrdersAPI" \
    --description "Order processing REST API" \
    --output json | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

echo "Created API Gateway with ID: $API_ID"

# Get root resource
ROOT_RESOURCE_ID=$(aws --endpoint-url="$ENDPOINT" --region="$REGION" apigateway get-resources \
    --rest-api-id "$API_ID" \
    --output json | python3 -c "import sys,json; items=json.load(sys.stdin)['items']; print([i['id'] for i in items if i['path']=='/'][0])")

# Create /orders resource
ORDERS_RESOURCE_ID=$(aws --endpoint-url="$ENDPOINT" --region="$REGION" apigateway create-resource \
    --rest-api-id "$API_ID" \
    --parent-id "$ROOT_RESOURCE_ID" \
    --path-part "orders" \
    --output json | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

echo "Created /orders resource: $ORDERS_RESOURCE_ID"

# Create GET /orders/{order_id} resource
ORDER_ID_RESOURCE_ID=$(aws --endpoint-url="$ENDPOINT" --region="$REGION" apigateway create-resource \
    --rest-api-id "$API_ID" \
    --parent-id "$ORDERS_RESOURCE_ID" \
    --path-part "{order_id}" \
    --output json | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

echo "Created /orders/{order_id} resource: $ORDER_ID_RESOURCE_ID"

# Create POST method on /orders
aws --endpoint-url="$ENDPOINT" --region="$REGION" apigateway put-method \
    --rest-api-id "$API_ID" \
    --resource-id "$ORDERS_RESOURCE_ID" \
    --http-method POST \
    --authorization-type NONE \
    --output json > /dev/null

# Create GET method on /orders/{order_id}
aws --endpoint-url="$ENDPOINT" --region="$REGION" apigateway put-method \
    --rest-api-id "$API_ID" \
    --resource-id "$ORDER_ID_RESOURCE_ID" \
    --http-method GET \
    --authorization-type NONE \
    --output json > /dev/null

CREATOR_LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:OrderCreator"

# Integrate POST /orders with OrderCreator Lambda
aws --endpoint-url="$ENDPOINT" --region="$REGION" apigateway put-integration \
    --rest-api-id "$API_ID" \
    --resource-id "$ORDERS_RESOURCE_ID" \
    --http-method POST \
    --type AWS_PROXY \
    --integration-http-method POST \
    --uri "arn:aws:apigateway:${REGION}:lambda:path/2015-03-31/functions/${CREATOR_LAMBDA_ARN}/invocations" \
    --output json > /dev/null

# Integrate GET /orders/{order_id} with OrderCreator Lambda (reuse for status lookup)
aws --endpoint-url="$ENDPOINT" --region="$REGION" apigateway put-integration \
    --rest-api-id "$API_ID" \
    --resource-id "$ORDER_ID_RESOURCE_ID" \
    --http-method GET \
    --type AWS_PROXY \
    --integration-http-method POST \
    --uri "arn:aws:apigateway:${REGION}:lambda:path/2015-03-31/functions/${CREATOR_LAMBDA_ARN}/invocations" \
    --output json > /dev/null

# Deploy the API to a "local" stage
aws --endpoint-url="$ENDPOINT" --region="$REGION" apigateway create-deployment \
    --rest-api-id "$API_ID" \
    --stage-name local \
    --output json > /dev/null

echo "API deployed. Endpoint: http://localhost:4566/restapis/${API_ID}/local/_user_request_/orders"

# Save API ID for use in tests
echo "$API_ID" > /tmp/api_gateway_id.txt
echo "export API_GATEWAY_ID=$API_ID" >> /etc/environment

# Grant API Gateway permission to invoke OrderCreator Lambda
aws --endpoint-url="$ENDPOINT" --region="$REGION" lambda add-permission \
    --function-name OrderCreator \
    --statement-id apigateway-invoke \
    --action lambda:InvokeFunction \
    --principal apigateway.amazonaws.com \
    --output json 2>/dev/null || echo "Permission may already exist, skipping."

echo ""
echo "AWS resource provisioning complete."
echo "API Gateway ID: $API_ID"
echo "SQS Queue URL: $QUEUE_URL"
echo "SNS Topic ARN: $SNS_TOPIC_ARN"
