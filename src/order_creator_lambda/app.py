"""
OrderCreator Lambda Function

Handles POST /orders requests via API Gateway.
- Validates input payload
- Persists order to PostgreSQL with PENDING status
- Publishes order_id to SQS OrderProcessingQueue
- Returns 202 Accepted with order_id
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import boto3
import psycopg2
from psycopg2.extras import RealDictCursor

# ---------------------------------------------------------------------------
# Logging setup - structured JSON compatible
# ---------------------------------------------------------------------------
log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=getattr(logging, log_level, logging.INFO),
)
logger = logging.getLogger("order_creator")

# ---------------------------------------------------------------------------
# Configuration from environment variables
# ---------------------------------------------------------------------------
DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ.get("DB_PORT", 5432))
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]

SQS_QUEUE_URL = os.environ["SQS_QUEUE_URL"]
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AWS_ENDPOINT_URL = os.environ.get("AWS_ENDPOINT_URL")  # set when using LocalStack


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db_connection():
    """Open and return a new database connection."""
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        connect_timeout=5,
        cursor_factory=RealDictCursor,
    )


def insert_order(conn, order_id: str, user_id: str, product_id: str, quantity: int) -> dict:
    """
    Insert a new order row with PENDING status.
    Returns the inserted row as a dict.
    """
    sql = """
        INSERT INTO orders (id, user_id, product_id, quantity, status)
        VALUES (%s, %s, %s, %s, 'PENDING')
        RETURNING id, user_id, product_id, quantity, status, created_at, updated_at
    """
    with conn.cursor() as cur:
        cur.execute(sql, (order_id, user_id, product_id, quantity))
        row = cur.fetchone()
    conn.commit()
    return dict(row)


# ---------------------------------------------------------------------------
# SQS helper
# ---------------------------------------------------------------------------
def get_sqs_client():
    """Return a boto3 SQS client, pointing at LocalStack when configured."""
    kwargs = {"region_name": AWS_REGION}
    if AWS_ENDPOINT_URL:
        kwargs["endpoint_url"] = AWS_ENDPOINT_URL
        kwargs["aws_access_key_id"] = os.environ.get("AWS_ACCESS_KEY_ID", "test")
        kwargs["aws_secret_access_key"] = os.environ.get("AWS_SECRET_ACCESS_KEY", "test")
    return boto3.client("sqs", **kwargs)


def publish_to_sqs(order_id: str) -> str:
    """
    Publish a message containing the order_id to OrderProcessingQueue.
    Returns the SQS MessageId.
    """
    sqs = get_sqs_client()
    message_body = json.dumps({"order_id": order_id})
    response = sqs.send_message(
        QueueUrl=SQS_QUEUE_URL,
        MessageBody=message_body,
        MessageAttributes={
            "source": {
                "StringValue": "OrderCreator",
                "DataType": "String",
            }
        },
    )
    return response["MessageId"]


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
def validate_payload(body: dict) -> list[str]:
    """
    Validate the order request payload.
    Returns a list of validation error messages; empty list means valid.
    """
    errors = []

    if not body.get("user_id"):
        errors.append("user_id is required and must be a non-empty string.")
    elif not isinstance(body["user_id"], str):
        errors.append("user_id must be a string.")

    if not body.get("product_id"):
        errors.append("product_id is required and must be a non-empty string.")
    elif not isinstance(body["product_id"], str):
        errors.append("product_id must be a string.")

    quantity = body.get("quantity")
    if quantity is None:
        errors.append("quantity is required.")
    elif not isinstance(quantity, int) or isinstance(quantity, bool):
        errors.append("quantity must be an integer.")
    elif quantity <= 0:
        errors.append("quantity must be a positive integer greater than zero.")

    return errors


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------
def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------
def handler(event, context):
    """
    Entry point for API Gateway proxy integration.

    Accepts:
        POST /orders  { "user_id": str, "product_id": str, "quantity": int }

    Returns:
        GET /orders/{order_id}  -> order status lookup
        Everything else        -> 405 Method Not Allowed
    """
    logger.info("Received event: %s", json.dumps(event))

    http_method = event.get("httpMethod", "")
    path = event.get("path", "")
    path_parameters = event.get("pathParameters") or {}

    # ------------------------------------------------------------------
    # GET /orders/{order_id}  -- order status lookup
    # ------------------------------------------------------------------
    if http_method == "GET" and path_parameters.get("order_id"):
        order_id = path_parameters["order_id"]
        logger.info("Status lookup for order_id=%s", order_id)

        try:
            conn = get_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
                    row = cur.fetchone()
            finally:
                conn.close()
        except Exception as exc:
            logger.error("Database error during status lookup: %s", exc, exc_info=True)
            return _response(503, {"error": "Database unavailable. Please try again later."})

        if not row:
            return _response(404, {"error": f"Order {order_id} not found."})

        order = dict(row)
        order["created_at"] = order["created_at"].isoformat()
        order["updated_at"] = order["updated_at"].isoformat()
        return _response(200, order)

    # ------------------------------------------------------------------
    # POST /orders  -- order creation
    # ------------------------------------------------------------------
    if http_method != "POST":
        return _response(405, {"error": f"Method {http_method} not allowed. Use POST."})

    # Parse body
    raw_body = event.get("body") or "{}"
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        logger.warning("Invalid JSON body: %s", raw_body)
        return _response(400, {"error": "Request body must be valid JSON."})

    if not isinstance(body, dict):
        return _response(400, {"error": "Request body must be a JSON object."})

    # Validate
    errors = validate_payload(body)
    if errors:
        logger.warning("Validation failed: %s", errors)
        return _response(400, {"error": "Validation failed.", "details": errors})

    user_id = body["user_id"].strip()
    product_id = body["product_id"].strip()
    quantity = int(body["quantity"])
    order_id = str(uuid.uuid4())

    logger.info(
        "Creating order order_id=%s user_id=%s product_id=%s quantity=%d",
        order_id, user_id, product_id, quantity,
    )

    # Persist order
    try:
        conn = get_db_connection()
        try:
            order = insert_order(conn, order_id, user_id, product_id, quantity)
        finally:
            conn.close()
    except Exception as exc:
        logger.error("Failed to insert order: %s", exc, exc_info=True)
        return _response(503, {"error": "Database unavailable. Please try again later."})

    logger.info("Order persisted: order_id=%s", order_id)

    # Publish to SQS
    try:
        message_id = publish_to_sqs(order_id)
        logger.info("Published to SQS: order_id=%s message_id=%s", order_id, message_id)
    except Exception as exc:
        logger.error("Failed to publish to SQS: %s", exc, exc_info=True)
        # Order is already in DB - do not return 5xx; warn instead
        # In production a retry / outbox pattern would handle this
        return _response(202, {
            "order_id": order_id,
            "status": "PENDING",
            "message": "Order accepted. Queue publish failed - processing may be delayed.",
        })

    return _response(202, {
        "order_id": order_id,
        "status": "PENDING",
        "message": "Order accepted and queued for processing.",
    })
