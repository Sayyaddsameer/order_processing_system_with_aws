# API Documentation

## Order Management API

This internal API is designed for robustness. It utilizes asynchronous processing to handle requests instantly under load.

### `POST /orders`

Create a new order asynchronously.

**Request Body (application/json)**
```json
{
  "user_id": "string (Required): The unique identifier of the user placing the order.",
  "product_id": "string (Required): The unique identifier of the product.",
  "quantity": "integer (Required): Number of items ordered. Must be > 0."
}
```

**Responses**

* `202 Accepted` - Order submitted and enqueued successfully.
```json
{
  "order_id": "d748f2b7-1c6e-4e42-aca3-92f3a6167ef2",
  "status": "PENDING",
  "message": "Order accepted and queued for processing."
}
```

* `400 Bad Request` - Validation error on the input payload.
```json
{
  "error": "Validation failed.",
  "details": ["quantity must be a positive integer greater than zero."]
}
```

* `405 Method Not Allowed` - Client used a method other than POST.

* `503 Service Unavailable` - Immediate database connection failure or AWS downstream error preventing order enrollment.

---

### `GET /orders/{order_id}`

Retrieve the current status of a specific order. Since order processing is asynchronous and system-independent, clients should use this endpoint to poll or verify the final state.

**Path Parameters**
* `order_id` (string) - The UUID returned from the `POST /orders` endpoint.

**Responses**

* `200 OK` - Order found.
```json
{
  "id": "d748f2b7-1c6e-4e42-aca3-92f3a6167ef2",
  "user_id": "u-123",
  "product_id": "p-456",
  "quantity": 2,
  "status": "CONFIRMED",
  "created_at": "2023-10-27T10:00:00+00:00",
  "updated_at": "2023-10-27T10:00:02+00:00"
}
```
*Note: `status` may be `PENDING`, `CONFIRMED`, or `FAILED` depending on downstream async processing results.*

* `404 Not Found` - The requested `order_id` does not exist in the database.
```json
{
  "error": "Order d748f2b7-1c6e-4e42-aca3-92f3a6167ef2 not found."
}
```

---

## Internal Event Specifications

*(For Documentation Only - Not publicly accessible over HTTP)*

### SQS `OrderProcessingQueue` Message Format
```json
{
  "order_id": "d748f2b7-1c6e-4e42-aca3-92f3a6167ef2"
}
```

### SNS `OrderStatusNotifications` Message Format
```json
{
  "order_id": "d748f2b7-1c6e-4e42-aca3-92f3a6167ef2",
  "new_status": "CONFIRMED"
}
```
*SNS Message Attributes are also injected including `event_type` and `new_status` for potential subscription filtering.*
