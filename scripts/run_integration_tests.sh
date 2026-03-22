#!/usr/bin/env bash
# Script to run integration tests locally

set -e

echo "Running integration tests..."

# Get the API ID dynamically from LocalStack
API_ID=$(aws --endpoint-url=http://localhost:4566 --region=us-east-1 apigateway get-rest-apis \
  --query 'items[0].id' --output text 2>/dev/null || echo "not-found")

if [ "$API_ID" == "not-found" ] || [ -z "$API_ID" ]; then
    echo "API Gateway not found. Ensure LocalStack is fully initialized."
    echo "You can check by running: awslocal apigateway get-rest-apis"
    exit 1
fi

export API_GATEWAY_ID=$API_ID
export API_GATEWAY_URL="http://localhost:4566/restapis/${API_ID}/local/_user_request_/orders"

echo "Using API_GATEWAY_URL: $API_GATEWAY_URL"

python3 -m pytest tests/integration/test_workflow.py -v -s
