# ses-inbound Lambda

SES receipt-rule Lambda action for provisioned agent email (inbound leg).
Reads the raw MIME email SES already stored in S3, parses it with the
stdlib `email` package, extracts SES's own SPF/DKIM/DMARC/spam/virus
verdicts, stores attachments to S3 with 24h presigned download URLs, and
POSTs a clean JSON payload to the Isol8 backend's `/webhooks/ses-inbound`
ingest endpoint.

One pip dependency, `dkimpy` (pure Python, plus `dnspython`), so each DKIM
signature is verified on its own; the CDK bundles `requirements.txt` into the
asset. `boto3`/`botocore` come from the Lambda runtime.

See `index.py`'s module docstring for the full design (failure posture,
attachment caps, why the backend—not this Lambda—makes every content
decision).

## Environment variables

| Variable | Purpose |
|----------|---------|
| `RAW_MAIL_BUCKET` | S3 bucket the paired SES S3 action stores raw MIME into. |
| `RAW_MAIL_PREFIX` | Key prefix under which the raw MIME lives (default `""`); the object key is `{RAW_MAIL_PREFIX}{messageId}`. |
| `ATTACHMENTS_BUCKET` | S3 bucket attachments are stored to, at `inbound/{messageId}/{filename}`. |
| `BACKEND_INGEST_URL` | Full URL of the backend's SES ingest endpoint (e.g. `https://api.isol8.co/api/v1/webhooks/ses-inbound`). |
| `SES_INBOUND_SECRET_ARN` | Secrets Manager ARN of the shared secret sent as `X-Lightbulb-SES-Secret`; fetched once and cached across warm invocations. |

## Testing

```bash
apps/backend/.venv/bin/python -m pytest apps/infra/lambda/ses-inbound -v
```
