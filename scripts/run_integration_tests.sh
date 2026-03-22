#!/usr/bin/env bash
# Script to run integration tests against the local LocalStack environment.
# All configuration is sourced from environment variables.

set -e

ENDPOINT="${AWS_ENDPOINT_URL:-http://localhost:4566}"
REGION="${AWS_REGION:-us-east-1}"
STAGE_NAME="${API_STAGE_NAME:-local}"

echo "Running integration tests..."
echo "  LocalStack endpoint : $ENDPOINT"
echo "  AWS Region          : $REGION"

# Resolve API Gateway ID dynamically from LocalStack
API_ID=$(aws --endpoint-url="$ENDPOINT" --region="$REGION" apigateway get-rest-apis \
  --query 'items[?name==`OrdersAPI`].id' --output text 2>/dev/null || echo "")

if [ -z "$API_ID" ] || [ "$API_ID" == "None" ]; then
    echo ""
    echo "ERROR: OrdersAPI Gateway not found in LocalStack."
    echo "  Ensure the stack is healthy: docker-compose logs localstack"
    echo "  Verify initialization: awslocal apigateway get-rest-apis"
    exit 1
fi

export API_GATEWAY_ID="$API_ID"
export API_GATEWAY_URL="${ENDPOINT}/restapis/${API_ID}/${STAGE_NAME}/_user_request_/orders"

echo "  API Gateway ID  : $API_GATEWAY_ID"
echo "  API Gateway URL : $API_GATEWAY_URL"
echo ""

python3 -m pytest tests/integration/ -v -s
