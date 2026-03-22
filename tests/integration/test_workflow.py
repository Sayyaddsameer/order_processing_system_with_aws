import os
import time
import uuid
import requests
import boto3
import psycopg2
import pytest

# Read config from env (set by our test-runner or manually)
API_GATEWAY_URL = os.environ.get(
    "API_GATEWAY_URL", 
    f"http://localhost:4566/restapis/{os.environ.get('API_GATEWAY_ID', 'not-set')}/local/_user_request_/orders"
)
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AWS_ENDPOINT_URL = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = int(os.environ.get("DB_PORT", 5432))
DB_NAME = os.environ.get("DB_NAME", "orders_db")
DB_USER = os.environ.get("DB_USER", "orders_user")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "orders_password")

@pytest.fixture(scope="module")
def db_conn():
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD
    )
    yield conn
    conn.close()

def test_full_order_processing_workflow(db_conn):
    """
    End-to-end integration test:
    1. Send POST /orders to API Gateway.
    2. Ensure HTTP 202 is returned with order_id.
    3. Check database to ensure order is initially PENDING (or already processed if fast).
    4. Wait for async processing.
    5. Check database to ensure order is CONFIRMED or FAILED.
    """
    
    # 1. Create order
    user_id = f"test-user-{uuid.uuid4().hex[:6]}"
    payload = {
        "user_id": user_id,
        "product_id": "test-product-999",
        "quantity": 10
    }
    
    response = requests.post(API_GATEWAY_URL, json=payload, timeout=5)
    
    # 2. Check sync response
    assert response.status_code == 202, f"Failed to create order: {response.text}"
    body = response.json()
    assert "order_id" in body
    order_id = body["order_id"]
    print(f"\\nCreated order {order_id}")

    # 3. Wait for async processing (SQS -> OrderProcessor)
    # We give it up to 15 seconds, checking every 1s
    max_retries = 15
    final_status = None
    
    with db_conn.cursor() as cur:
        for attempt in range(max_retries):
            # 4. Check database state
            cur.execute("SELECT status FROM orders WHERE id = %s", (order_id,))
            row = cur.fetchone()
            assert row is not None, "Order not found in database!"
            
            final_status = row[0]
            print(f"Attempt {attempt+1}: Order status is {final_status}")
            
            if final_status in ['CONFIRMED', 'FAILED']:
                break
                
            time.sleep(1)
            
    # 5. Assert final state
    assert final_status in ['CONFIRMED', 'FAILED'], f"Order processing stuck. Final status: {final_status}"
    
    # Note: We can also verify NotificationService logs via localstack CloudWatch APIs,
    # but verifying the DB status confirms the entire SQS -> Lambda pipeline worked.
    print(f"\\nIntegration test passed! Order processed successfully to status: {final_status}")
