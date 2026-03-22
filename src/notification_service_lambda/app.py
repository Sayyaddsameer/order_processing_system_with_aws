"""
NotificationService Lambda Function

Subscribed to the OrderStatusNotifications SNS topic.
Receives status change events and logs them, simulating email/SMS dispatch.
"""

import json
import logging
import os

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=getattr(logging, log_level, logging.INFO),
)
logger = logging.getLogger("notification_service")


# ---------------------------------------------------------------------------
# Notification simulation
# ---------------------------------------------------------------------------
def send_notification(order_id: str, new_status: str) -> None:
    """
    Simulate sending an external notification (email / SMS / push).
    In production this would call SES, SNS SMS, or a third-party service.
    """
    if new_status == "CONFIRMED":
        logger.info(
            "NOTIFICATION SENT: Order %s has been confirmed. "
            "Your order is being prepared for shipment.",
            order_id,
        )
    elif new_status == "FAILED":
        logger.warning(
            "NOTIFICATION SENT: Order %s processing failed. "
            "Please contact support or retry your order.",
            order_id,
        )
    else:
        logger.info(
            "NOTIFICATION SENT: Order %s status updated to %s.",
            order_id, new_status,
        )


# ---------------------------------------------------------------------------
# Per-record processing
# ---------------------------------------------------------------------------
def process_sns_record(record: dict) -> None:
    """
    Parse a single SNS record and dispatch the notification.
    SNS wraps messages inside an Sns key.
    """
    try:
        sns_envelope = record.get("Sns", {})
        raw_message = sns_envelope.get("Message", "{}")
        subject = sns_envelope.get("Subject", "")
        message_id = sns_envelope.get("MessageId", "unknown")

        logger.info("Received SNS message: message_id=%s subject=%s", message_id, subject)

        payload = json.loads(raw_message)
        order_id = payload.get("order_id")
        new_status = payload.get("new_status")

        if not order_id or not new_status:
            logger.error(
                "Malformed SNS payload missing order_id or new_status: %s", payload
            )
            return

        logger.info(
            "Order %s status updated to %s", order_id, new_status
        )

        send_notification(order_id, new_status)

    except json.JSONDecodeError as exc:
        logger.error("Failed to parse SNS message body: %s", exc)
    except Exception as exc:
        logger.error("Unexpected error processing SNS record: %s", exc, exc_info=True)
        raise


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------
def handler(event, context):
    """
    Entry point for SNS-triggered Lambda.
    Processes each SNS notification record individually.
    """
    records = event.get("Records", [])
    logger.info("NotificationService invoked with %d record(s).", len(records))

    for record in records:
        process_sns_record(record)

    logger.info("NotificationService processing complete.")
    return {"statusCode": 200, "processed": len(records)}
