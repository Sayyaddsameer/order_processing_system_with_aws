"""
OrderProcessor Lambda Function

Consumes messages from OrderProcessingQueue (SQS).
- Implements idempotency via the processed_messages table
- Simulates order processing (random success/failure with weighted probability)
- Updates order status to CONFIRMED or FAILED in the database
- Publishes status update event to OrderStatusNotifications SNS topic
"""

import json
import logging
import os
import random
import time

import boto3
import psycopg2
from psycopg2.extras import RealDictCursor

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=getattr(logging, log_level, logging.INFO),
)
logger = logging.getLogger("order_processor")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ.get("DB_PORT", 5432))
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]

SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AWS_ENDPOINT_URL = os.environ.get("AWS_ENDPOINT_URL")

# Probability that simulated processing succeeds (0.0 - 1.0)
SUCCESS_PROBABILITY = float(os.environ.get("PROCESSING_SUCCESS_RATE", "0.85"))


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        connect_timeout=5,
        cursor_factory=RealDictCursor,
    )


def is_message_processed(conn, message_id: str) -> bool:
    """
    Return True if this SQS message has already been successfully processed.
    This is the core of the idempotency mechanism.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM processed_messages WHERE message_id = %s",
            (message_id,),
        )
        return cur.fetchone() is not None


def mark_message_processed(conn, message_id: str) -> None:
    """Record that we have finished processing this SQS message."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO processed_messages (message_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (message_id,),
        )
    conn.commit()


def get_order(conn, order_id: str) -> dict | None:
    """Fetch an order row by primary key. Returns None if not found."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def update_order_status(conn, order_id: str, new_status: str) -> None:
    """Update the status of an order. The trigger handles updated_at."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE orders SET status = %s WHERE id = %s",
            (new_status, order_id),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# SNS helper
# ---------------------------------------------------------------------------
def get_sns_client():
    kwargs = {"region_name": AWS_REGION}
    if AWS_ENDPOINT_URL:
        kwargs["endpoint_url"] = AWS_ENDPOINT_URL
        kwargs["aws_access_key_id"] = os.environ.get("AWS_ACCESS_KEY_ID", "test")
        kwargs["aws_secret_access_key"] = os.environ.get("AWS_SECRET_ACCESS_KEY", "test")
    return boto3.client("sns", **kwargs)


def publish_status_update(order_id: str, new_status: str) -> str:
    """
    Publish an order status change event to the SNS topic.
    Returns the SNS MessageId.
    """
    sns = get_sns_client()
    payload = {"order_id": order_id, "new_status": new_status}
    response = sns.publish(
        TopicArn=SNS_TOPIC_ARN,
        Message=json.dumps(payload),
        Subject="OrderStatusUpdated",
        MessageAttributes={
            "event_type": {
                "StringValue": "ORDER_STATUS_UPDATED",
                "DataType": "String",
            },
            "new_status": {
                "StringValue": new_status,
                "DataType": "String",
            },
        },
    )
    return response["MessageId"]


# ---------------------------------------------------------------------------
# Processing simulation
# ---------------------------------------------------------------------------
def simulate_processing(order: dict) -> str:
    """
    Simulate business processing logic.
    In a real system this would call inventory, payment, shipping APIs.
    Returns 'CONFIRMED' or 'FAILED'.
    """
    logger.info(
        "Simulating processing for order_id=%s product_id=%s quantity=%d",
        order["id"], order["product_id"], order["quantity"],
    )
    # Simulate some work time
    time.sleep(random.uniform(0.1, 0.5))

    if random.random() < SUCCESS_PROBABILITY:
        return "CONFIRMED"
    return "FAILED"


# ---------------------------------------------------------------------------
# Per-record processing
# ---------------------------------------------------------------------------
def process_record(record: dict) -> None:
    """
    Process a single SQS record.
    Raises an exception on unrecoverable errors so SQS can route to DLQ.
    """
    message_id = record.get("messageId", "unknown")
    logger.info("Processing SQS record message_id=%s", message_id)

    # Parse message body
    try:
        body = json.loads(record["body"])
        order_id = body["order_id"]
    except (json.JSONDecodeError, KeyError) as exc:
        logger.error("Malformed SQS message message_id=%s: %s", message_id, exc)
        # Do not raise - a malformed message will never succeed; drop it
        return

    conn = get_db_connection()
    try:
        # Idempotency check
        if is_message_processed(conn, message_id):
            logger.warning(
                "Duplicate message detected message_id=%s order_id=%s - skipping.",
                message_id, order_id,
            )
            return

        # Retrieve order
        order = get_order(conn, order_id)
        if not order:
            logger.error("Order not found in database: order_id=%s", order_id)
            # Mark as processed to avoid infinite retries on a missing order
            mark_message_processed(conn, message_id)
            return

        # Guard: only process orders that are still PENDING
        if order["status"] != "PENDING":
            logger.warning(
                "Order order_id=%s already in status=%s - skipping.",
                order_id, order["status"],
            )
            mark_message_processed(conn, message_id)
            return

        # Run simulated business logic
        new_status = simulate_processing(order)
        logger.info("Processing result: order_id=%s new_status=%s", order_id, new_status)

        # Persist status update
        update_order_status(conn, order_id, new_status)
        logger.info("Order status updated in DB: order_id=%s status=%s", order_id, new_status)

        # Mark message as processed BEFORE publishing to SNS.
        # If SNS publish fails we may re-publish on retry, but that is
        # safer than double-processing the order.
        mark_message_processed(conn, message_id)

    except Exception as exc:
        logger.error(
            "Failed to process order order_id=%s: %s", order_id, exc, exc_info=True
        )
        conn.rollback()
        conn.close()
        raise  # Let SQS retry / DLQ handle it

    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Publish SNS notification (outside DB transaction)
    try:
        sns_message_id = publish_status_update(order_id, new_status)
        logger.info(
            "Published SNS notification: order_id=%s new_status=%s sns_message_id=%s",
            order_id, new_status, sns_message_id,
        )
    except Exception as exc:
        logger.error(
            "Failed to publish SNS notification for order_id=%s: %s",
            order_id, exc, exc_info=True,
        )
        # Status is already updated in DB; log and continue.
        # In production an outbox/retry pattern would ensure delivery.


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------
def handler(event, context):
    """
    Entry point for SQS-triggered Lambda.
    Processes each SQS record individually.
    Returns a batchItemFailures list so partial batch success is supported.
    """
    logger.info("OrderProcessor invoked with %d record(s).", len(event.get("Records", [])))

    batch_item_failures = []

    for record in event.get("Records", []):
        message_id = record.get("messageId", "unknown")
        try:
            process_record(record)
        except Exception as exc:
            logger.error(
                "Unhandled error for message_id=%s: %s", message_id, exc, exc_info=True
            )
            # Report this item as failed so SQS will retry it (or route to DLQ)
            batch_item_failures.append({"itemIdentifier": message_id})

    if batch_item_failures:
        logger.warning(
            "%d record(s) failed processing and will be retried.",
            len(batch_item_failures),
        )

    return {"batchItemFailures": batch_item_failures}
