"""
Unit tests for OrderProcessor Lambda.
Environment variables are mocked before import so no real AWS/DB connections are made.
"""
import json
import pytest
from unittest.mock import MagicMock, patch

_ENV_MOCK = {
    "DB_HOST": "localhost",
    "DB_PORT": "5432",
    "DB_NAME": "orders_db",
    "DB_USER": "orders_user",
    "DB_PASSWORD": "orders_password",
    "SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:000000000000:OrderStatusNotifications",
    "AWS_REGION": "us-east-1",
    "AWS_ENDPOINT_URL": "http://localhost:4566",
    "PROCESSING_SUCCESS_RATE": "1.0",  # Force deterministic success for tests
    "LOG_LEVEL": "ERROR",
}

import sys
import os

with patch.dict("os.environ", _ENV_MOCK):
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../src/order_processor_lambda")))
    from app import handler, process_record, is_message_processed


# ---------------------------------------------------------------------------
# Helper: build a fake SQS record
# ---------------------------------------------------------------------------
def _sqs_record(order_id: str, message_id: str = "msg-001") -> dict:
    return {
        "messageId": message_id,
        "body": json.dumps({"order_id": order_id}),
    }


# ---------------------------------------------------------------------------
# process_record tests
# ---------------------------------------------------------------------------
class TestProcessRecord:
    @patch("app.get_db_connection")
    @patch("app.publish_status_update")
    def test_new_message_is_processed(self, mock_publish, mock_get_db):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_db.return_value = mock_conn

        call_count = [0]

        def execute_side_effect(query, params=None):
            call_count[0] += 1
            if "processed_messages WHERE" in query:
                mock_cursor.fetchone.return_value = None  # not yet processed
            elif "SELECT * FROM orders" in query:
                mock_cursor.fetchone.return_value = {
                    "id": "order-001",
                    "user_id": "u1",
                    "product_id": "p1",
                    "quantity": 1,
                    "status": "PENDING",
                }

        mock_cursor.execute.side_effect = execute_side_effect
        mock_publish.return_value = "sns-msg-001"

        process_record(_sqs_record("order-001"))

        mock_publish.assert_called_once_with("order-001", "CONFIRMED")
        mock_conn.commit.assert_called()

    @patch("app.get_db_connection")
    def test_duplicate_message_is_skipped(self, mock_get_db):
        """Idempotency: already-processed message must not trigger DB updates."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_db.return_value = mock_conn

        # Simulate idempotency table returning a hit
        mock_cursor.fetchone.return_value = (1,)

        process_record(_sqs_record("order-duplicate", "msg-dup"))

        # Only one DB call should happen (the idempotency check)
        assert mock_cursor.execute.call_count == 1

    @patch("app.get_db_connection")
    def test_malformed_message_does_not_raise(self, mock_get_db):
        """Malformed SQS body must be silently dropped (not retried endlessly)."""
        mock_conn = MagicMock()
        mock_get_db.return_value = mock_conn

        record = {"messageId": "msg-bad", "body": "not-valid-json"}
        # Should not raise
        process_record(record)

    @patch("app.get_db_connection")
    def test_order_not_in_db_is_skipped(self, mock_get_db):
        """An order_id that doesn't exist in DB must be gracefully handled."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_db.return_value = mock_conn

        def execute_side_effect(query, params=None):
            if "processed_messages WHERE" in query:
                mock_cursor.fetchone.return_value = None  # not processed
            elif "SELECT * FROM orders" in query:
                mock_cursor.fetchone.return_value = None  # order missing

        mock_cursor.execute.side_effect = execute_side_effect
        # Should not raise
        process_record(_sqs_record("order-missing"))

    @patch("app.get_db_connection")
    def test_already_confirmed_order_is_skipped(self, mock_get_db):
        """An order already CONFIRMED must not be double-processed."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_db.return_value = mock_conn

        def execute_side_effect(query, params=None):
            if "processed_messages WHERE" in query:
                mock_cursor.fetchone.return_value = None
            elif "SELECT * FROM orders" in query:
                mock_cursor.fetchone.return_value = {
                    "id": "order-done", "status": "CONFIRMED",
                    "user_id": "u1", "product_id": "p1", "quantity": 1,
                }

        mock_cursor.execute.side_effect = execute_side_effect
        with patch("app.publish_status_update") as mock_publish:
            process_record(_sqs_record("order-done"))
            mock_publish.assert_not_called()


# ---------------------------------------------------------------------------
# handler tests
# ---------------------------------------------------------------------------
class TestHandler:
    def test_batch_failures_returned_on_exception(self):
        event = {
            "Records": [
                {"messageId": "msg-1", "body": json.dumps({"order_id": "o1"})},
                {"messageId": "msg-2", "body": json.dumps({"order_id": "o2"})},
            ]
        }
        with patch("app.process_record", side_effect=Exception("DB Error")):
            result = handler(event, None)

        assert "batchItemFailures" in result
        assert len(result["batchItemFailures"]) == 2
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-1"
        assert result["batchItemFailures"][1]["itemIdentifier"] == "msg-2"

    def test_partial_batch_failure(self):
        """Only failing records should appear in batchItemFailures."""
        event = {
            "Records": [
                {"messageId": "msg-ok", "body": json.dumps({"order_id": "o-ok"})},
                {"messageId": "msg-fail", "body": json.dumps({"order_id": "o-fail"})},
            ]
        }

        def side_effect(record):
            if record["messageId"] == "msg-fail":
                raise Exception("Simulated failure")

        with patch("app.process_record", side_effect=side_effect):
            result = handler(event, None)

        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-fail"

    def test_empty_event_returns_no_failures(self):
        result = handler({"Records": []}, None)
        assert result["batchItemFailures"] == []
