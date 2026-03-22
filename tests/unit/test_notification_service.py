import json
import logging
import pytest
from unittest.mock import patch

import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../src/notification_service_lambda')))
from app import handler

def test_handler_processes_sns_records(caplog):
    caplog.set_level(logging.INFO)

    event = {
        "Records": [
            {
                "EventSource": "aws:sns",
                "Sns": {
                    "MessageId": "sns-123",
                    "Subject": "OrderStatusUpdated",
                    "Message": json.dumps({
                        "order_id": "order-456",
                        "new_status": "CONFIRMED"
                    })
                }
            }
        ]
    }

    response = handler(event, None)

    assert response["statusCode"] == 200
    assert response["processed"] == 1
    
    # Check if notification was simulated
    assert any("NOTIFICATION SENT: Order order-456 has been confirmed" in record.message for record in caplog.records)

def test_handler_handles_invalid_json(caplog):
    event = {
        "Records": [
            {
                "EventSource": "aws:sns",
                "Sns": {
                    "MessageId": "sns-123",
                    "Message": "invalid json!!"
                }
            }
        ]
    }

    response = handler(event, None)

    assert response["statusCode"] == 200
    assert response["processed"] == 1
    assert any("Failed to parse SNS message body" in record.message for record in caplog.records)
