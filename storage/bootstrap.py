"""Reconcile the dedicated OCR bucket, identities and expiration policy."""

from __future__ import annotations

import json
import os
import time
from urllib.parse import unquote

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


def client(service: str, endpoint: str, access: str, secret: str):
    """Create an explicit-credential client with bounded bootstrap requests."""
    return boto3.client(
        service,
        endpoint_url=endpoint,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4" if service == "s3" else "v4",
            s3={"addressing_style": "path"},
            connect_timeout=2,
            read_timeout=5,
            retries={"total_max_attempts": 1},
        ),
    )


def ensure_user(iam, name: str, access: str, secret: str, policy: dict) -> None:
    """Keep deterministic deployment keys using SeaweedFS's IAM extension."""
    try:
        iam.create_user(UserName=name)
    except ClientError as error:
        if error.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
    keys = iam.list_access_keys(UserName=name)["AccessKeyMetadata"]
    if not any(key["AccessKeyId"] == access for key in keys):
        # AWS has no caller-supplied key parameters. Inject the documented
        # SeaweedFS extension before botocore signs the request; no custom crypto.
        def supply_key(params, **kwargs):
            params["body"].update(AccessKeyId=access, SecretAccessKey=secret)

        event = "before-call.iam.CreateAccessKey"
        iam.meta.events.register(event, supply_key, unique_id="ocr-bootstrap-key")
        try:
            iam.create_access_key(UserName=name)
        finally:
            iam.meta.events.unregister(event, unique_id="ocr-bootstrap-key")
    policy_name = name + "-archive"
    arn = "arn:aws:iam:::policy/" + policy_name
    document = json.dumps(policy)
    try:
        version = iam.get_policy(PolicyArn=arn)["Policy"]["DefaultVersionId"]
        current = iam.get_policy_version(
            PolicyArn=arn,
            VersionId=version,
        )["PolicyVersion"]["Document"]
    except ClientError as error:
        if error.response["Error"]["Code"] != "NoSuchEntity":
            raise
        iam.create_policy(PolicyName=policy_name, PolicyDocument=document)
    else:
        if isinstance(current, str):
            current = json.loads(unquote(current))
        if current != policy:
            # SeaweedFS keeps one current v1, with no historical versions.
            iam.create_policy_version(
                PolicyArn=arn,
                PolicyDocument=document,
                SetAsDefault=True,
            )
    iam.attach_user_policy(UserName=name, PolicyArn=arn)
    # Authenticate the configured pair before reporting success or revoking old
    # keys. The writer's bucket-scoped ListBucket policy is required for atomic
    # archive-name collision checks; SeaweedFS may expose that bucket in this
    # account-level response even though the policy grants no bucket creation.
    probe = client("s3", iam.meta.endpoint_url, access, secret)
    try:
        probe.list_buckets()
    except ClientError:
        raise RuntimeError(
            "Configured identity credentials are invalid. Rotate both the access "
            "key and secret together, then rerun initialization."
        ) from None
    finally:
        probe.close()
    # These dedicated users are owned by this deployment. A new configured
    # access ID intentionally rotates their key after validating its replacement.
    for key in keys:
        if key["AccessKeyId"] != access:
            iam.delete_access_key(UserName=name, AccessKeyId=key["AccessKeyId"])


def policies(bucket: str) -> tuple[dict, dict]:
    """Return narrowly scoped writer and operator-reader policies."""
    # New archives start with the uploaded filename at the bucket root. The
    # bucket is dedicated to OCR output, so object permissions cover its keys.
    resources = [f"arn:aws:s3:::{bucket}/*"]
    listing = {
        "Effect": "Allow",
        "Action": ["s3:ListBucket"],
        "Resource": [f"arn:aws:s3:::{bucket}"],
    }
    writer = {
        "Version": "2012-10-17",
        "Statement": [
            listing,
            {"Effect": "Allow", "Action": ["s3:PutObject"], "Resource": resources},
        ],
    }
    reader = {
        "Version": "2012-10-17",
        "Statement": [
            listing,
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": resources,
            },
        ],
    }
    return writer, reader


def provision(env) -> dict:
    """Provision from explicit configuration; return a secret-free summary."""
    required = (
        "OCR_S3_ADMIN_ACCESS_KEY",
        "OCR_S3_ADMIN_SECRET_KEY",
        "OCR_S3_ACCESS_KEY",
        "OCR_S3_SECRET_KEY",
        "OCR_S3_READER_ACCESS_KEY",
        "OCR_S3_READER_SECRET_KEY",
        "OCR_S3_BROWSER_ACCESS_KEY",
        "OCR_S3_BROWSER_SECRET_KEY",
    )
    if any(not env.get(name, "").strip() for name in required):
        raise ValueError("Missing storage credentials; run configure_storage.py")
    bucket = env.get("OCR_S3_BUCKET", "ocr-outputs")
    days = int(env.get("OCR_S3_RETENTION_DAYS", "30"))
    if days < 0:
        raise ValueError("Retention must be zero (disabled) or a positive day count")
    endpoint = env.get("OCR_S3_ENDPOINT", "http://seaweedfs:8333")
    credentials = (endpoint, env[required[0]], env[required[1]])
    s3, iam = client("s3", *credentials), client("iam", *credentials)
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                s3.list_buckets()
                break
            except (BotoCoreError, ClientError):
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "SeaweedFS was not ready within 60 seconds"
                    ) from None
                time.sleep(1)
        try:
            s3.create_bucket(Bucket=bucket)
        except ClientError as error:
            if error.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
                raise
        writer, reader = policies(bucket)
        ensure_user(iam, "ocr-writer", env[required[2]], env[required[3]], writer)
        ensure_user(iam, "ocr-reader", env[required[4]], env[required[5]], reader)
        browser = {
            "Version": "2012-10-17",
            "Statement": [
                reader["Statement"][0],
                {
                    **reader["Statement"][1],
                    "Action": ["s3:GetObject", "s3:DeleteObject"],
                },
            ],
        }
        ensure_user(iam, "ocr-browser", env[required[6]], env[required[7]], browser)
        if days:
            s3.put_bucket_lifecycle_configuration(
                Bucket=bucket,
                LifecycleConfiguration={
                    "Rules": [
                        {
                            "ID": "ocr-expiration",
                            "Status": "Enabled",
                            "Filter": {},
                            "Expiration": {"Days": days},
                        }
                    ]
                },
            )
        else:
            s3.delete_bucket_lifecycle(Bucket=bucket)
        from ocrserver.storage_index import rebuild_index

        indexed = rebuild_index(s3, bucket)
        return {
            "bucket": bucket,
            "retention_days": days,
            "identities": 3,
            "indexed_archives": indexed,
        }
    finally:
        iam.close()
        s3.close()


def main() -> None:
    """Provision storage from environment settings with sanitized failure output."""
    try:
        result = provision(os.environ)
    except ClientError as error:
        # Do not print requests, responses, or exception messages with keys.
        raise SystemExit(
            "Storage initialization failed: " + error.response["Error"]["Code"]
        ) from None
    except RuntimeError as error:
        raise SystemExit("Storage initialization failed: " + str(error)) from None
    except (BotoCoreError, ValueError) as error:
        raise SystemExit(
            "Storage initialization failed: " + type(error).__name__
        ) from None
    print(json.dumps({"storage_initialized": True, **result}))


if __name__ == "__main__":
    main()
