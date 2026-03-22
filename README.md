# E-Commerce Order Processing System (Event-Driven Architecture)

A resilient, event-driven backend system for an e-commerce platform that processes orders asynchronously using AWS Serverless technologies (Lambda, API Gateway, SQS, SNS) and PostgreSQL. 

The architecture decouples order submission from downstream processing, ensuring low latency for users and high availability even under heavy loads. The system simulates a production-grade AWS environment locally using LocalStack and Docker Compose.

**Watch the demo:**  
[![Watch the Demo](https://img.shields.io/badge/Watch-Demo%20Video-red?style=for-the-badge&logo=google-drive)](https://drive.google.com/file/d/1Gf41-IXdyxP3dFF7yFBjgm9iV-mVJU-Z/view?usp=sharing)

---

## Architecture Overview

```
User
 |
 v
API Gateway  (POST /orders)
 |
 v
OrderCreator Lambda
 |-- PostgreSQL (RDS) : INSERT order, status=PENDING
 |-- SQS: OrderProcessingQueue
       |
       v
   OrderProcessor Lambda  (async, SQS-triggered)
    |-- PostgreSQL (RDS) : UPDATE order status=CONFIRMED|FAILED
    |-- SNS: OrderStatusNotifications
                  |
                  v
          NotificationService Lambda
           (logs email/SMS mock)
```

### Components
1. **API Gateway**: Provides the REST endpoint (`POST /orders`).
2. **OrderCreator Lambda**: Synchronously validates requests, persists the initial `PENDING` order state to PostgreSQL, and enqueues a message to SQS. Returns `202 Accepted` immediately.
3. **SQS (OrderProcessingQueue)**: Provides durable message persistence, decoupling the creator from processor. Includes a Dead-Letter Queue (DLQ) for failed messages.
4. **OrderProcessor Lambda**: Asynchronously consumes SQS messages. Implements idempotency to handle duplicate events safely. Simulates business logic, updates the DB state to `CONFIRMED` or `FAILED`, and fires an event to SNS.
5. **SNS (OrderStatusNotifications)**: Publishes status update events to subscribers (fan-out pattern).
6. **NotificationService Lambda**: Subscribes to the SNS topic and logs status changes, acting as a mock for email/SMS microservices.
7. **RDS (PostgreSQL)**: Persistent relational store for order state.

---

## Setup Instructions

### Prerequisites
* Docker & Docker Compose
* Python 3.11+ (for local testing)
* `awscli-local` (`pip install awscli-local` or `awslocal` alias)

### 1. Start the Environment
Run the entire stack (PostgreSQL, LocalStack, and all Lambda built services):
```bash
docker-compose up -d
```
*Note: The first startup takes a moment as LocalStack provisions resources via `scripts/init_aws.sh` and PostgreSQL runs `scripts/init_db.sql`.*

You can monitor the AWS initialization:
```bash
docker-compose logs -f localstack
```

### 2. Verify Initialization
Check if the API Gateway has been deployed by finding its ID:
```bash
awslocal apigateway get-rest-apis
```
You should see `OrdersAPI` in the list, and the `init_aws.sh` script automatically deploys it to the `local` stage.

### 3. Usage & Testing

#### Send a Request
Grab the `API_ID` from the logs (or the command above) and use `awslocal` or `curl`:
```bash
export API_ID=$(awslocal apigateway get-rest-apis --query 'items[0].id' --output text)
curl -X POST "http://localhost:4566/restapis/${API_ID}/local/_user_request_/orders" \
     -H "Content-Type: application/json" \
     -d '{"user_id": "u-123", "product_id": "p-456", "quantity": 2}'
```
**Expected Response:** `202 Accepted`
```json
{"order_id": "uuid-here", "status": "PENDING", "message": "Order accepted and queued for processing."}
```

#### Check Order Status
```bash
curl "http://localhost:4566/restapis/${API_ID}/local/_user_request_/orders/uuid-here"
```

#### View AWS Services
* **SQS Queues**: `awslocal sqs list-queues`
* **SNS Topics**: `awslocal sns list-topics`
* **Lambda Logs**: Inspect LocalStack output for the mock notification `docker-compose logs localstack | grep "NOTIFICATION SENT"`

---

## Running Automated Tests

All automated tests use `pytest`. We have Unit Tests and an End-to-End Integration workflow.

### Install Dependencies
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r tests/requirements.txt
```

### Run Unit Tests
```bash
pytest tests/unit/ -v
```

### Run Integration Tests
The integration test sends a real HTTP request to the local API Gateway and validates the asynchronous database updates.
```bash
# Ensure docker-compose is running
./scripts/run_integration_tests.sh
```

---

## Key Design Principles Handled

* **Idempotency**: `OrderProcessor` uses a database-backed idempotency key (SQS `messageId`) to ensure messages are processed exactly once even if SQS delivers them multiple times.
* **Eventual Consistency**: The REST API returns a `PENDING` state synchronously before the order is actually confirmed.
* **Dead-Letter Routing**: Failed processing requests are safely routed to a DLQ after 3 retries.
* **Partial Batch Failures**: Using `batchItemFailures` in the `OrderProcessor` Lambda ensures SQS properly retries only the failed records.

## Project Structure
* `src/`: Lambda function source code and Dockerfiles.
* `tests/`: Unit and End-to-End integration tests.
* `infrastructure/`: AWS SAM template (`template.yaml`).
* `scripts/`: Initialization and helper scripts.
* `docker-compose.yml`: Local multi-container orchestration.
