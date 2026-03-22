import os
import time
import uuid
import requests
import boto3
import psycopg2
import pytest

# ---------------------------------------------------------------------------
# Configuration — all from environment variables (set by test-runner or .env)
# ---------------------------------------------------------------------------
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AWS_ENDPOINT_URL = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "test")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "test")
API_STAGE_NAME = os.environ.get("API_STAGE_NAME", "local")

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = int(os.environ.get("DB_PORT", 5432))
DB_NAME = os.environ.get("DB_NAME", "orders_db")
DB_USER = os.environ.get("DB_USER", "orders_user")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "orders_password")


def _boto_kwargs():
    """Return standard boto3 kwargs for LocalStack-aware clients."""
    return {
        "region_name": AWS_REGION,
        "endpoint_url": AWS_ENDPOINT_URL,
        "aws_access_key_id": AWS_ACCESS_KEY_ID,
        "aws_secret_access_key": AWS_SECRET_ACCESS_KEY,
    }


def _resolve_api_gateway_url() -> str:
    """
    Resolve the API Gateway URL dynamically.
    Prefers the API_GATEWAY_URL env var; otherwise derives it from LocalStack.
    """
    if os.environ.get("API_GATEWAY_URL"):
        return os.environ["API_GATEWAY_URL"]

    api_id = os.environ.get("API_GATEWAY_ID")
    if not api_id:
        # Discover from LocalStack by API name
        client = boto3.client("apigateway", **_boto_kwargs())
        apis = client.get_rest_apis().get("items", [])
        matches = [a["id"] for a in apis if a["name"] == "OrdersAPI"]
        if not matches:
            pytest.fail(
                "OrdersAPI not found in LocalStack. "
                "Ensure docker-compose is up and init_aws.sh ran successfully."
            )
        api_id = matches[0]

    return f"{AWS_ENDPOINT_URL}/restapis/{api_id}/{API_STAGE_NAME}/_user_request_/orders"


# Resolve once at module load
API_GATEWAY_URL = _resolve_api_gateway_url()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def db_conn():
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_full_order_processing_workflow(db_conn):
    """
    End-to-end integration test:
    1. Send POST /orders to API Gateway.
    2. Ensure HTTP 202 is returned with order_id.
    3. Wait for async processing (SQS -> OrderProcessor).
    4. Verify order reaches CONFIRMED or FAILED in the database.
    """
    user_id = f"test-user-{uuid.uuid4().hex[:8]}"
    payload = {
        "user_id": user_id,
        "product_id": "test-product-001",
        "quantity": 3,
    }

    # 1. Submit order
    response = requests.post(API_GATEWAY_URL, json=payload, timeout=10)

    # 2. Verify immediate 202 response
    assert response.status_code == 202, (
        f"Expected 202, got {response.status_code}: {response.text}"
    )
    body = response.json()
    assert "order_id" in body, f"Missing order_id in response: {body}"
    order_id = body["order_id"]
    print(f"\nCreated order: {order_id}")

    # 3. Poll database for async processing completion (up to 20 seconds)
    max_retries = 20
    final_status = None

    with db_conn.cursor() as cur:
        for attempt in range(max_retries):
            cur.execute("SELECT status FROM orders WHERE id = %s", (order_id,))
            row = cur.fetchone()
            assert row is not None, f"Order {order_id} not found in database!"

            final_status = row[0]
            print(f"Attempt {attempt + 1}: status = {final_status}")

            if final_status in ("CONFIRMED", "FAILED"):
                break

            time.sleep(1)

    # 4. Assert terminal state reached
    assert final_status in ("CONFIRMED", "FAILED"), (
        f"Order stuck at status '{final_status}' after {max_retries}s"
    )
    print(f"\nWorkflow test passed. Final status: {final_status}")


def test_post_orders_validation_rejects_bad_input():
    """
    Verify that the OrderCreator Lambda returns 400 for invalid payloads.
    """
    bad_payloads = [
        {},  # completely empty
        {"user_id": "u1", "product_id": "p1"},  # missing quantity
        {"user_id": "u1", "product_id": "p1", "quantity": 0},  # zero quantity
        {"user_id": "u1", "product_id": "p1", "quantity": -1},  # negative quantity
        {"user_id": "", "product_id": "p1", "quantity": 1},  # empty user_id
    ]

    for payload in bad_payloads:
        response = requests.post(API_GATEWAY_URL, json=payload, timeout=10)
        assert response.status_code == 400, (
            f"Expected 400 for payload {payload}, got {response.status_code}: {response.text}"
        )


def test_notification_service_logged():
    """
    Verify that the NotificationService Lambda produced a 'NOTIFICATION SENT'
    log entry via LocalStack CloudWatch Logs, confirming the SNS fan-out worked.
    """
    logs_client = boto3.client("logs", **_boto_kwargs())
    log_group = "/aws/lambda/NotificationService"

    # Allow a short settle time after the workflow test
    time.sleep(3)

    try:
        streams_resp = logs_client.describe_log_streams(
            logGroupName=log_group,
            orderBy="LastEventTime",
            descending=True,
            limit=5,
        )
    except logs_client.exceptions.ResourceNotFoundException:
        pytest.skip(
            "CloudWatch log group not yet created by LocalStack. "
            "Run this test after test_full_order_processing_workflow."
        )

    assert streams_resp["logStreams"], (
        "No log streams found for NotificationService Lambda"
    )

    events_resp = logs_client.get_log_events(
        logGroupName=log_group,
        logStreamName=streams_resp["logStreams"][0]["logStreamName"],
        startFromHead=False,
    )
    log_text = " ".join(e["message"] for e in events_resp.get("events", []))

    assert "NOTIFICATION SENT" in log_text, (
        f"Expected 'NOTIFICATION SENT' in NotificationService logs. "
        f"Got: {log_text[:500]}"
    )
    print("\nNotification log verification passed!")
