# Turant Bazaar

A minimal grocery/local-vendor delivery app, built as a working reference for how a small business can launch on AWS — from architecture to CI/CD.

Live demo: `https://bazaar.prashantgunpal.in` *(temporary — check GitHub for status)*

## Why this exists

Most beginner AWS projects stop at "I launched an EC2 instance." This one goes further — a real request flow (browser → DNS → load balancer → app server → database), deployed with the same patterns a small production app would use, and documented so anyone can copy the approach for their own small business.

## Architecture

```
Internet → Route 53 → ALB (public subnet) → EC2 (private subnet) → RDS Aurora MySQL (private subnet)
                                                  ↓
                                         S3 (images) → CloudFront
```

- **VPC** across 2 Availability Zones, each with a public subnet (ALB) and private subnets (app + database) — see `/docs/architecture.png`
- **EC2** runs the app inside a Docker container, behind an **Application Load Balancer**
- **RDS Aurora MySQL** stores products, vendors, and orders — private subnet, no public access
- **S3** stores product images, served fast via **CloudFront**
- **Secrets Manager** holds the DB credentials — never hardcoded in code
- **IAM roles** give the EC2 instance only the access it needs (Secrets Manager read, S3 read)
- **GitHub Actions** builds and deploys automatically on every push to `main`

## Why these choices (not just what)

- **EC2 is in a private subnet, not public** — even though it hosts the app, only the ALB should be internet-facing. If EC2 were public, anyone could bypass the load balancer and hit the app server directly.
- **RDS is in its own private subnet, separate from EC2** — extra isolation. If the app subnet's security group is ever misconfigured, the database subnet is still a separate boundary.
- **RDS has no outbound internet access** — a database has no legitimate reason to reach the internet. This is enforced through its security group, not the route table (the route table just makes a path *available*; the security group decides who's *allowed* to use it).
- **Aurora MySQL, not DynamoDB** — this app's data (orders, products, users) is relational. DynamoDB only earns its place if there's a genuine need for very high-frequency writes (e.g. live location tracking every 1–2 seconds), which this project doesn't have.

## Common mistakes this project avoids

- Putting the database in a public subnet — never do this
- Hardcoding DB passwords or API keys in code — use Secrets Manager
- Giving EC2 broad "Full Access" IAM policies — least privilege only
- Trusting client-sent totals on checkout — the backend recalculates the order total from the database, not from what the frontend sends
- Single-AZ everything — no redundancy if a zone has an issue

## How to deploy this yourself

1. **VPC** — create with 2 AZs, public + private-app + private-db subnets in each
2. **IAM** — create an EC2 role with `SecretsManagerReadWrite` (scoped to this secret) and `S3ReadOnlyAccess`
3. **RDS** — launch Aurora MySQL in the private-db subnets, run `schema.sql` to create tables
4. **Secrets Manager** — store the DB credentials, name it `turantbazaar/db-creds`
5. **S3** — create a bucket for images, note the name
6. **EC2** — launch in the private-app subnet, attach the IAM role, install Docker
7. **ALB** — create in the public subnets, target group pointing to the EC2 instance, health check on `/health`
8. **Route 53** — point your domain to the ALB
9. **ACM** — request a certificate, attach it to the ALB listener for HTTPS
10. **GitHub Actions** — add `EC2_HOST`, `EC2_USER`, `EC2_SSH_KEY` as repo secrets; push to `main` to deploy

## Local development

```bash
pip install -r requirements.txt
export DB_HOST=localhost DB_USER=root DB_PASSWORD=yourpass DB_NAME=turantbazaar
mysql -u root -p < schema.sql
python app.py
```

## Tech stack

Backend: Python, Flask · Database: Aurora MySQL · Infra: AWS (VPC, EC2, ALB, RDS, S3, CloudFront, Route 53, ACM, Secrets Manager, IAM) · CI/CD: GitHub Actions · Containerization: Docker
