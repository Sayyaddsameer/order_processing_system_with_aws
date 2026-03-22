"""
Unit tests for OrderCreator Lambda.
Environment variables are mocked before import so no real AWS/DB connections are made.
"""
import json
import pytest
from unittest.mock import MagicMock, patch

# Mock environment before importing the Lambda module
_ENV_MOCK = {
    "DB_HOST": "localhost",
    "DB_PORT": "5432",
    "DB_NAME": "orders_db",
    "DB_USER": "orders_user",
    "DB_PASSWORD": "orders_password",
    "SQS_QUEUE_URL": "http://localhost:4566/000000000000/OrderProcessingQueue",
    "AWS_REGION": "us-east-1",
    "AWS_ENDPOINT_URL": "http://localhost:4566",
    "LOG_LEVEL": "ERROR",
}

import sys
import os

with patch.dict("os.environ", _ENV_MOCK):
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../src/order_creator_lambda")))
    from app import handler, validate_payload


# ---------------------------------------------------------------------------
# validate_payload tests
# ---------------------------------------------------------------------------
class TestValidatePayload:
    def test_valid_payload(self):
        body = {"user_id": "user-123", "product_id": "prod-456", "quantity": 2}
        assert validate_payload(body) == []

    def test_missing_user_id(self):
        errors = validate_payload({"product_id": "p1", "quantity": 1})
        assert any("user_id" in e for e in errors)

    def test_missing_product_id(self):
        errors = validate_payload({"user_id": "u1", "quantity": 1})
        assert any("product_id" in e for e in errors)

    def test_missing_quantity(self):
        errors = validate_payload({"user_id": "u1", "product_id": "p1"})
        assert any("quantity" in e for e in errors)

    def test_quantity_zero(self):
        errors = validate_payload({"user_id": "u1", "product_id": "p1", "quantity": 0})
        assert any("positive integer" in e for e in errors)

    def test_quantity_negative(self):
        errors = validate_payload({"user_id": "u1", "product_id": "p1", "quantity": -5})
        assert any("positive integer" in e for e in errors)

    def test_quantity_is_bool(self):
        errors = validate_payload({"user_id": "u1", "product_id": "p1", "quantity": True})
        assert any("integer" in e for e in errors)

    def test_quantity_is_float_string(self):
        errors = validate_payload({"user_id": "u1", "product_id": "p1", "quantity": "2"})
        assert any("integer" in e for e in errors)

    def test_empty_user_id(self):
        errors = validate_payload({"user_id": "", "product_id": "p1", "quantity": 1})
        assert any("user_id" in e for e in errors)


# ---------------------------------------------------------------------------
# handler tests
# ---------------------------------------------------------------------------
class TestHandler:
    def _make_post_event(self, body: dict) -> dict:
        return {
            "httpMethod": "POST",
            "path": "/orders",
            "pathParameters": None,
            "body": json.dumps(body),
        }

    @patch("app.get_db_connection")
    @patch("app.publish_to_sqs")
    def test_success_returns_202(self, mock_publish, mock_get_db):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_db.return_value = mock_conn
        mock_cursor.fetchone.return_value = {
            "id": "test-uuid",
            "user_id": "user-123",
            "product_id": "prod-456",
            "quantity": 2,
            "status": "PENDING",
            "created_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-01T00:00:00+00:00",
        }
        mock_publish.return_value = "msg-id-123"

        response = handler(self._make_post_event({"user_id": "user-123", "product_id": "prod-456", "quantity": 2}), None)

        assert response["statusCode"] == 202
        body = json.loads(response["body"])
        assert "order_id" in body
        assert body["status"] == "PENDING"
        mock_publish.assert_called_once()
        mock_conn.commit.assert_called_once()

    def test_invalid_json_returns_400(self):
        event = {"httpMethod": "POST", "path": "/orders", "pathParameters": None, "body": "not-json{{"}
        response = handler(event, None)
        assert response["statusCode"] == 400
        assert "valid JSON" in json.loads(response["body"])["error"]

    def test_validation_failure_returns_400(self):
        response = handler(self._make_post_event({"user_id": "u1", "quantity": -1}), None)
        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "Validation failed" in body["error"]
        assert "details" in body

    def test_wrong_method_returns_405(self):
        event = {"httpMethod": "DELETE", "path": "/orders", "pathParameters": None, "body": "{}"}
        response = handler(event, None)
        assert response["statusCode"] == 405

    @patch("app.get_db_connection")
    def test_db_failure_returns_503(self, mock_get_db):
        mock_get_db.side_effect = Exception("Connection refused")
        response = handler(self._make_post_event({"user_id": "u1", "product_id": "p1", "quantity": 1}), None)
        assert response["statusCode"] == 503

    @patch("app.get_db_connection")
    @patch("app.publish_to_sqs")
    def test_sqs_failure_still_returns_202(self, mock_publish, mock_get_db):
        """Order is persisted; SQS failure should not cause a 5xx."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_db.return_value = mock_conn
        mock_cursor.fetchone.return_value = {
            "id": "test-uuid", "user_id": "u1", "product_id": "p1",
            "quantity": 1, "status": "PENDING",
            "created_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-01T00:00:00+00:00",
        }
        mock_publish.side_effect = Exception("SQS unavailable")

        response = handler(self._make_post_event({"user_id": "u1", "product_id": "p1", "quantity": 1}), None)
        assert response["statusCode"] == 202
