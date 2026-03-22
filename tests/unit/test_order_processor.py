import json
import pytest
from unittest.mock import MagicMock, patch

with patch.dict('os.environ', {
    'DB_HOST': 'localhost',
    'DB_NAME': 'orders_db',
    'DB_USER': 'orders_user',
    'DB_PASSWORD': 'orders_password',
    'SNS_TOPIC_ARN': 'arn:aws:sns:us-east-1:000000000000:topic',
    'AWS_REGION': 'us-east-1',
    'PROCESSING_SUCCESS_RATE': '1.0' # Force success for testing
}):
    import sys
    import os
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../src/order_processor_lambda')))
    from app import handler, process_record

@patch('app.get_db_connection')
@patch('app.publish_status_update')
def test_process_record_success_idempotent(mock_publish, mock_get_db):
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_get_db.return_value = mock_conn

    # Setup DB mock returns
    def cursor_execute_side_effect(query, params=None):
        if "processed_messages WHERE message_id" in query:
            # Idempotency check: return None (meaning not processed)
            mock_cursor.fetchone.return_value = None
        elif "SELECT * FROM orders" in query:
            # Order lookup: return PENDING order
            mock_cursor.fetchone.return_value = {
                'id': 'order-123',
                'user_id': 'user',
                'product_id': 'prod',
                'quantity': 1,
                'status': 'PENDING'
            }

    mock_cursor.execute.side_effect = cursor_execute_side_effect
    mock_publish.return_value = 'sns-msg-123'

    record = {
        "messageId": "sqs-msg-123",
        "body": json.dumps({"order_id": "order-123"})
    }

    # Should not raise exception
    process_record(record)

    # Asserts
    assert mock_cursor.execute.call_count >= 4 # idempotency check, gets order, updates status, marks processed
    mock_publish.assert_called_once_with('order-123', 'CONFIRMED')
    mock_conn.commit.assert_called()

@patch('app.get_db_connection')
def test_process_record_already_processed(mock_get_db):
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_get_db.return_value = mock_conn

    # Idempotency check returns a row (meaning already processed)
    mock_cursor.fetchone.return_value = (1,)

    record = {
        "messageId": "sqs-msg-123",
        "body": json.dumps({"order_id": "order-123"})
    }

    process_record(record)

    # Verify we did NOT try to fetch the order or update it
    # execute would only be called once for the idempotency check
    assert mock_cursor.execute.call_count == 1

def test_handler_batch_failures():
    # If a record raises an exception, the handler should return it in batchItemFailures
    event = {
        "Records": [
            {"messageId": "msg-1"}, # Will fail (mocked)
            {"messageId": "msg-2"}  # Will fail
        ]
    }
    
    with patch('app.process_record', side_effect=Exception("DB Error")):
        result = handler(event, None)
        
    assert "batchItemFailures" in result
    assert len(result["batchItemFailures"]) == 2
    assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-1"
