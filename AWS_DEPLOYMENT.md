# AWS Deployment Guide

This document explains how to take the LocalStack-based Order Processing System and deploy it to a real AWS environment using the provided AWS Serverless Application Model (SAM) `template.yaml`.

## Is `template.yaml` Enough?

Yes, the included `infrastructure/template.yaml` is a complete AWS SAM template that defines the Serverless infrastructure (API Gateway, Lambda, SQS, SNS). However, the Lambdas require a PostgreSQL database. In a production AWS environment, you would use **Amazon RDS** or **Amazon Aurora Serverless**. 

While the SAM template *could* define the RDS instance, deploying relational databases via Serverless frameworks is generally discouraged due to slow provisioning times, statefulness, and VPC complexities. Instead, the template assumes the database already exists and accepts its connection details securely via **AWS Systems Manager (SSM) Parameter Store**.

---

## Prerequisites for AWS Deployment

1. **AWS Account**: You need an active AWS account.
2. **AWS CLI & SAM CLI**: 
   * Install the [AWS CLI](https://aws.amazon.com/cli/).
   * Install the [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html).
   * Configure your credentials using `aws configure`.
3. **Docker**: SAM uses Docker to build the Python deployment packages natively.

---

## Deployment Steps

### Step 1: Provision the Database (RDS)

Before deploying the serverless components, set up your database:
1. Go to the **Amazon RDS Console**.
2. Create a new **PostgreSQL** database (e.g., PostgreSQL 15). 
   * *Tip:* For a portfolio project, use the "Free Tier" template.
3. Configure the master username (e.g., `orders_user`) and a strong master password.
4. Ensure the RDS instance is placed in subnets that your Lambdas can access (or make it publicly accessible for testing purposes *only*, though VPC deployment is best practice).
5. Once the database is `Available`, note down its **Endpoint Address**.
6. Connect to the database using `psql` or a visual client (like DBeaver) and run the `scripts/init_db.sql` script to create the `orders` and `processed_messages` tables.

### Step 2: Store Database Secrets Securely

The SAM template uses AWS Systems Manager (SSM) to securely resolve the database password at deployment time (`'{{resolve:ssm:orders_db_password:1}}'`).

Store the password in SSM using the AWS CLI:
```bash
aws ssm put-parameter \
    --name "orders_db_password" \
    --value "YOUR_SUPER_SECRET_PASSWORD" \
    --type "SecureString"
```

### Step 3: Build the Application

Navigate to the `infrastructure` directory (where `template.yaml` is located) and run the SAM build command. This packages your Python code and dependencies:

```bash
cd infrastructure
sam build --use-container
```
*Note: `--use-container` ensures that packages with C-extensions (like `psycopg2`) are compiled correctly for the Linux environment that Lambda runs in.*

### Step 4: Deploy the Application

Run the guided deploy command to provision the API Gateway, SQS queues, SNS topic, and Lambda functions:

```bash
sam deploy --guided
```

You will be prompted for several parameters:
* **Stack Name**: Choose a name like `order-processing-system`.
* **AWS Region**: e.g., `us-east-1`.
* **Parameter DatabaseHost**: Enter the RDS Endpoint Address you noted in Step 1.
* **Confirm changes before deploy**: `Y` (Allows you to review the IAM roles and resources before creation).
* **Allow SAM CLI IAM role creation**: `Y`.
* **Disable rollback**: `N`.
* **OrderCreatorFunction CreateOrderApi may not have authorization defined, Is this okay?**: `Y` (For this demo, the API is public).

### Step 5: Subscribe to the SNS Topic (Optional)

The system publishes order status updates to the `OrderStatusNotifications` SNS topic. By default, the `NotificationServiceFunction` Lambda is subscribed. If you want to receive real emails or SMS for testing:
1. Go to the **Amazon SNS Console**.
2. Find the newly created `OrderStatusNotifications` topic.
3. Click **Create subscription**.
4. Choose **Email** or **SMS** as the protocol and enter your email address/phone number.
5. If using Email, check your inbox and confirm the subscription.

### Step 6: Test the AWS Deployment

Once deployment completes, the SAM CLI will output the `OrdersApiUrl`. 

Use `curl` or Postman to test the live API:
```bash
curl -X POST "https://<YOUR_API_ID>.execute-api.<REGION>.amazonaws.com/Prod/orders" \
     -H "Content-Type: application/json" \
     -d '{"user_id": "prod-user-1", "product_id": "prod-item-99", "quantity": 1}'
```

---

## VPC Considerations (Production Ready)

By default, the provided `template.yaml` deploys Lambdas **outside** of a Virtual Private Cloud (VPC). 

If you configure your RDS database to be private (not publicly accessible—which is highly recommended for production security), you must update the `template.yaml` to attach the Lambdas to your VPC so they can reach the database.

**Add the VpcConfig properties to the `Globals` section of `template.yaml`:**
```yaml
Globals:
  Function:
    Timeout: 30
    MemorySize: 256
    Runtime: python3.11
    VpcConfig:
      SecurityGroupIds:
        - sg-0123456789abcdef0 # A security group that allows outbound access to RDS and AWS Services (SQS/SNS)
      SubnetIds:
        - subnet-0123456789abcdef0 # Private subnets
        - subnet-abcdef01234567890
```

*Important:* If Lambdas are placed in a private VPC subnet, they lose direct internet access. Because they need to communicate with AWS SQS and SNS, you MUST configure **VPC Endpoints** for SQS and SNS (or provide a NAT Gateway) so the Lambdas can route traffic to those AWS services. 

## Clean Up

To avoid incurring future AWS charges, tear down the serverless stack when finished:
```bash
sam delete --stack-name order-processing-system
```
Finally, delete your RDS database manually via the AWS Console.
