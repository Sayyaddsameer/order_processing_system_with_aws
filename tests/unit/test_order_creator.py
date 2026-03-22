import json
import pytest
from unittest.mock import MagicMock, patch

# Important: Mock environment variables before importing app
with patch.dict('os.environ', {
    'DB_HOST': 'localhost',
    'DB_NAME': 'orders_db',
    'DB_USER': 'orders_user',
    'DB_PASSWORD': 'orders_password',
    'SQS_QUEUE_URL': 'http://dummy.url/queue',
    'AWS_REGION': 'us-east-1',
}):
    # Import the lambda handler
    import sys
    import os
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../src/order_creator_lambda')))
    from app import handler, validate_payload


def test_validate_payload_valid():
    body = {
        "user_id": "user123",
        "product_id": "prod456",
        "quantity": 2
    }
    errors = validate_payload(body)
    assert len(errors) == 0

def test_validate_payload_missing_fields():
    body = {"quantity": 2}
    errors = validate_payload(body)
    assert len(errors) == 2
    assert any("user_id is required" in e for e in errors)
    assert any("product_id is required" in e for e in errors)

def test_validate_payload_invalid_quantity():
    body = {
        "user_id": "user123",
        "product_id": "prod456",
        "quantity": -5
    }
    errors = validate_payload(body)
    assert len(errors) == 1
    assert any("quantity must be a positive integer" in e for e in errors)

@patch('app.get_db_connection')
@patch('app.publish_to_sqs')
def test_handler_post_orders_success(mock_publish, mock_get_db):
    # Mock DB connection
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_get_db.return_value = mock_conn

    # Mock DB insert result
    mock_cursor.fetchone.return_value = {
        'id': 'test-uuid',
        'user_id': 'user123',
        'product_id': 'prod456',
        'quantity': 2,
        'status': 'PENDING',
        'created_at': '2023-01-01T00:00:00Z',
        'updated_at': '2023-01-01T00:00:00Z'
    }

    # Mock SQS publish result
    mock_publish.return_value = 'msg-123'

    # Build event
    event = {
        "httpMethod": "POST",
        "path": "/orders",
        "body": json.dumps({
            "user_id": "user123",
            "product_id": "prod456",
            "quantity": 2
        })
    }

    # Execute
    response = handler(event, None)

    # Assert logic
    assert response["statusCode"] == 202
    body = json.loads(response["body"])
    assert "order_id" in body
    assert body["status"] == "PENDING"
    
    mock_publish.assert_called_once()
    mock_conn.commit.assert_called_once()


def test_handler_post_orders_invalid_json():
    event = {
        "httpMethod": "POST",
        "path": "/orders",
        "body": "invalid json {}}"
    }

    response = handler(event, None)

    assert response["statusCode"] == 400
    body = json.loads(response["body"])
    assert "error" in body
    assert "valid JSON" in body["error"]
