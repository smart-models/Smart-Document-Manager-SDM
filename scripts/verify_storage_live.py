"""Verify real OCR/S3 integration; fault stage briefly stops local SeaweedFS.

Run from the repository with development dependencies installed. Reports contain
only synthetic/calibration metadata, never credentials or document contents.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
import re
import secrets
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import dotenv_values
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Direct script execution needs the repository root before local imports.
from ocrserver.archive_names import safe_filename  # noqa: E402
from storage.bootstrap import client, ensure_user, policies, provision  # noqa: E402


def require(condition, message):
    """Raise RuntimeError when a live acceptance condition fails."""
    if not condition:
        raise RuntimeError(message)


def docker(*args, input_text=None, timeout=60):
    """Run one bounded Compose operation and reject unsuccessful exit status."""
    result = subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        input=input_text,
        timeout=timeout,
        check=False,
    )
    require(result.returncode == 0, "Docker operation failed: " + args[0])
    return result.stdout


class Verification:
    """Exercise live storage permissions, archival, browsing and fault recovery."""

    def __init__(self, env_file):
        """Resolve live endpoints and construct separate role-specific S3 clients."""
        self.env = {**dotenv_values(env_file), **os.environ}
        self.bucket = self.env.get("OCR_S3_BUCKET", "ocr-outputs")
        self.endpoint = f"http://127.0.0.1:{self.env.get('OCR_S3_PORT', '8333')}"
        self.api = f"http://127.0.0.1:{self.env.get('OCR_SERVER_PORT', '8020')}"
        token = self.env.get("UPSTREAM_CLIENT_TOKEN", "") or ""
        self.headers = {"Authorization": "Bearer " + token} if token else {}
        self.admin = self.s3("OCR_S3_ADMIN")
        self.writer = self.s3("OCR_S3")
        self.reader = self.s3("OCR_S3_READER")
        self.browser = self.s3("OCR_S3_BROWSER")
        self.records = []
        self.plain = (ROOT / "calibration/plain_text.png").read_bytes()

    def s3(self, prefix):
        """Construct an S3 client with credentials for the requested configured role."""
        return client(
            "s3",
            self.endpoint,
            self.env[prefix + "_ACCESS_KEY"],
            self.env[prefix + "_SECRET_KEY"],
        )

    def get(self, key):
        """Read an object through the reader role and always close its response body."""
        response = self.reader.get_object(Bucket=self.bucket, Key=key)
        try:
            return response["Body"].read()
        finally:
            response["Body"].close()

    def denied(self, actor, operation, **kwargs):
        """Require a specific operation to fail with an S3 AccessDenied response."""
        try:
            getattr(actor, operation)(**kwargs)
        except ClientError as error:
            require(
                error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 403
                and error.response.get("Error", {}).get("Code", "AccessDenied")
                == "AccessDenied",
                operation + " failed with an unexpected error",
            )
        else:
            raise RuntimeError("Unexpected permission: " + operation)

    def infrastructure(self):
        """Verify repeatable provisioning, S3 round trips and role permissions."""
        env = {**self.env, "OCR_S3_ENDPOINT": self.endpoint}
        first, second = provision(env), provision(env)
        require(first == second, "Bootstrap is not repeatable")
        key = "parse/verification/permissions.json"
        self.writer.put_object(Bucket=self.bucket, Key=key, Body=b'{"test":true}')
        require(self.get(key) == b'{"test":true}', "S3 round trip differs")
        try:
            self.writer.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=b'{"collision":true}',
                IfNoneMatch="*",
            )
        except ClientError as error:
            require(
                error.response.get("Error", {}).get("Code") == "PreconditionFailed",
                "Conditional collision check returned an unexpected error",
            )
        else:
            raise RuntimeError("Conditional collision check overwrote an object")
        require(self.get(key) == b'{"test":true}', "Collision changed existing data")
        for operation, kwargs in [
            ("get_object", {"Bucket": self.bucket, "Key": key}),
            ("delete_object", {"Bucket": self.bucket, "Key": key}),
            ("create_bucket", {"Bucket": "ocr-writer-must-not-create"}),
            (
                "put_bucket_lifecycle_configuration",
                {
                    "Bucket": self.bucket,
                    "LifecycleConfiguration": {
                        "Rules": [
                            {
                                "ID": "denied",
                                "Status": "Enabled",
                                "Filter": {},
                                "Expiration": {"Days": 1},
                            }
                        ]
                    },
                },
            ),
        ]:
            self.denied(self.writer, operation, **kwargs)
        require(
            any(
                item["Key"] == key
                for item in self.writer.list_objects_v2(
                    Bucket=self.bucket, Prefix=key, MaxKeys=1
                ).get("Contents", [])
            ),
            "Writer cannot check a candidate archive prefix",
        )
        writer_buckets = self.writer.list_buckets().get("Buckets", [])
        require(
            [item["Name"] for item in writer_buckets] == [self.bucket],
            "Writer sees buckets outside its dedicated bucket",
        )
        self.denied(self.reader, "put_object", Bucket=self.bucket, Key=key, Body=b"x")
        self.denied(self.reader, "delete_object", Bucket=self.bucket, Key=key)
        self.denied(self.browser, "put_object", Bucket=self.bucket, Key=key, Body=b"x")
        browser_object = self.browser.get_object(Bucket=self.bucket, Key=key)
        try:
            require(
                browser_object["Body"].read() == b'{"test":true}',
                "Browser read differs",
            )
        finally:
            browser_object["Body"].close()
        self.browser.delete_objects(
            Bucket=self.bucket, Delete={"Objects": [{"Key": key}]}
        )
        require(
            not self.reader.list_objects_v2(Bucket=self.bucket, Prefix=key).get(
                "Contents"
            ),
            "Browser deletion failed",
        )
        iam = client(
            "iam",
            self.endpoint,
            self.env["OCR_S3_ACCESS_KEY"],
            self.env["OCR_S3_SECRET_KEY"],
        )
        try:
            self.denied(iam, "list_users")
        finally:
            iam.close()
        require(
            httpx.get(self.endpoint, timeout=5).status_code == 403,
            "Anonymous S3 permitted",
        )
        console = httpx.get(
            f"http://127.0.0.1:{self.env.get('OCR_STORAGE_CONSOLE_PORT', '23646')}",
            timeout=5,
            follow_redirects=False,
        )
        require(
            console.status_code in (302, 303, 307, 401),
            "Console must require authentication",
        )
        with httpx.Client(timeout=10, follow_redirects=True) as browser:
            login_url = str(console.url.join("/login"))
            form = browser.get(login_url)
            token = re.search(r'name="csrf_token" value="([^"]+)"', form.text)
            require(token is not None, "Console login form unavailable")
            logged_in = browser.post(
                login_url,
                data={
                    "username": "reader",
                    "password": self.env["OCR_SEAWEED_READER_PASSWORD"],
                    "csrf_token": token.group(1),
                },
            )
            require(
                logged_in.status_code == 200 and "logout" in logged_in.text.lower(),
                "Console reader login failed",
            )
        rules = self.admin.get_bucket_lifecycle_configuration(Bucket=self.bucket)[
            "Rules"
        ]
        require(
            rules[0]["Expiration"]["Days"]
            == int(self.env.get("OCR_S3_RETENTION_DAYS", 30)),
            "Retention differs",
        )
        networks = json.loads(
            subprocess.run(
                ["docker", "inspect", "smart-document-manager", "document-storage-s3"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
        require(
            not set(networks[0]["NetworkSettings"]["Networks"])
            & set(networks[1]["NetworkSettings"]["Networks"]),
            "API can reach internal storage network",
        )
        self.admin.delete_object(Bucket=self.bucket, Key=key)
        self.rotation()
        return {
            "bootstrap_repeated": True,
            "writer_put_reader_get": True,
            "forbidden_operations_denied": True,
            "browser_get_delete_allowed_put_denied": True,
            "writer_list_buckets": "dedicated bucket only",
            "anonymous_s3": 403,
            "console_unauthenticated": console.status_code,
            "console_reader_login": True,
            "internal_network_isolated": True,
            "credential_rotation_verified": True,
            "retention_days": rules[0]["Expiration"]["Days"],
        }

    def rotation(self):
        """Rotate only an ephemeral test identity, never the running API user."""
        iam = client(
            "iam",
            self.endpoint,
            self.env["OCR_S3_ADMIN_ACCESS_KEY"],
            self.env["OCR_S3_ADMIN_SECRET_KEY"],
        )
        name = "ocr-verification-" + secrets.token_hex(6)
        arn = "arn:aws:iam:::policy/" + name + "-archive"
        old_access, new_access = secrets.token_hex(12), secrets.token_hex(12)
        secret, replacement = secrets.token_hex(24), secrets.token_hex(24)
        policy = policies(self.bucket)[0]
        try:
            ensure_user(iam, name, old_access, secret, policy)
            try:
                ensure_user(iam, name, old_access, replacement, policy)
            except RuntimeError:
                pass
            else:
                raise RuntimeError("Mismatched secret was accepted by initializer")
            ensure_user(iam, name, new_access, replacement, policy)
            keys = iam.list_access_keys(UserName=name)["AccessKeyMetadata"]
            require(
                [key["AccessKeyId"] for key in keys] == [new_access],
                "Old key not revoked",
            )
            old = client("s3", self.endpoint, old_access, secret)
            try:
                try:
                    old.list_buckets()
                except ClientError as error:
                    require(
                        error.response["ResponseMetadata"]["HTTPStatusCode"] == 403,
                        "Unexpected revoked-key response",
                    )
                else:
                    raise RuntimeError("Revoked key still authenticates")
            finally:
                old.close()
        finally:
            iam.detach_user_policy(UserName=name, PolicyArn=arn)
            iam.delete_user(UserName=name)
            iam.delete_policy(PolicyArn=arn)
            iam.close()

    def check_archive(self, response, source, *, require_images=False):
        """Validate committed archive metadata, object hashes and source identity."""
        require(
            response.status_code == 200, "OCR HTTP status " + str(response.status_code)
        )
        require(
            response.headers.get("x-ocr-storage-status") == "stored",
            "Archive not confirmed",
        )
        key = response.headers["x-ocr-archive-key"]
        manifest = json.loads(self.get(key))
        require(manifest["complete"], "Manifest incomplete")
        require(not manifest["partial_ocr"], "OCR returned partial content")
        require(
            manifest["source"]["sha256"] == hashlib.sha256(source).hexdigest(),
            "Source hash differs",
        )
        require(
            manifest["job_id"] == response.headers["x-ocr-job-id"], "Job ID differs"
        )
        require(
            re.fullmatch(r"[^/]+__\d{8}T\d{12}Z__[A-Za-z0-9_-]{8}/manifest\.json", key)
            is not None
            and key.rsplit("__", 1)[-1].removesuffix("/manifest.json")
            == manifest["job_id"],
            "Archive key does not follow the filename/date/code format",
        )
        if manifest["source"].get("original_name"):
            expected_name = safe_filename(
                manifest["source"]["original_name"],
                manifest["source"].get("extension", "bin"),
            )
            require(
                key.split("/", 1)[0].rsplit("__", 2)[0] == expected_name,
                "Archive filename does not correspond to the source filename",
            )
        blobs = {}
        for artifact in manifest["artifacts"]:
            blob = self.get(artifact["key"])
            require(
                hashlib.sha256(blob).hexdigest() == artifact["sha256"],
                "Artifact digest differs",
            )
            require(len(blob) == artifact["size"], "Artifact size differs")
            blobs[artifact["name"]] = blob
        require(
            not any(name.startswith("input.") for name in blobs),
            "Original unexpectedly stored",
        )
        require(bool(blobs.get("document.md")), "Markdown missing")
        payload = response.json()
        kind = manifest["kind"]
        expected = json.loads(response.content)
        if kind == "parse":
            expected.pop("json_rag", None)
            for page in expected["pages"]:
                page.pop("images", None)
                page.pop("image_names", None)
            require(
                json.loads(blobs["json_ocr.json"]) == expected,
                "Archived OCR JSON differs",
            )
        elif kind == "parse-chunks":
            require(
                blobs["json_chunks.json"] == response.content,
                "Canonical RAG bytes differ",
            )
            require(
                set(payload) == {"chunks"} and payload["chunks"], "RAG contract differs"
            )
            require("parsed.json" in blobs, "Intermediate parse missing")
        else:
            require(
                "json_ocr.json" not in blobs and "json_chunks.json" not in blobs,
                "OCR unexpectedly stored a JSON result",
            )
        for mapping in manifest["images"]:
            png = blobs[mapping["path"]]
            require(png.startswith(b"\x89PNG\r\n\x1a\n"), "Figure is not PNG")
            with Image.open(io.BytesIO(png)) as image:
                image.verify()
            require(
                mapping["path"].encode() in blobs["document.md"],
                "Figure reference unresolved",
            )
        if require_images:
            require(bool(manifest["images"]), "Fixture produced no figure crops")
            require(
                all(page.get("images") is None for page in payload["pages"]),
                "Images exposed despite false flag",
            )
        record = {
            "kind": manifest["kind"],
            "job_id": manifest["job_id"],
            "manifest_key": key,
            "prefix": key.removesuffix("manifest.json"),
            "created_at": manifest["created_at"],
            "objects": len(blobs) + 1,
            "bytes": sum(map(len, blobs.values())),
            "figures": len(manifest["images"]),
            "ocr_ms": manifest["processing_time_ms"],
            "archive_warnings": len(manifest.get("archive_warnings", [])),
        }
        self.records.append(record)
        return record

    async def check_browser_archive(self, http, record):
        """Verify archive browsing, Markdown preview and download consistency."""
        detail = await http.get("/storage/archive", params={"prefix": record["prefix"]})
        require(detail.status_code == 200, "Storage browser cannot open new archive")
        files = detail.json()["files"]
        document = next((item for item in files if item["name"] == "document.md"), None)
        require(document is not None, "Storage browser archive omits Markdown")
        preview = await http.get(
            "/storage/file", params={"key": document["key"], "preview": "true"}
        )
        require(
            preview.status_code == 200 and preview.content,
            "Storage browser cannot preview archived Markdown",
        )
        cursor = None
        found = False
        while True:
            params = {
                "kind": record["kind"],
                "date": record["created_at"][:10],
                "limit": 50,
            }
            if cursor:
                params["cursor"] = cursor
            listing = await http.get("/storage/archives", params=params)
            require(listing.status_code == 200, "Storage browser cannot list archives")
            page = listing.json()
            found = found or any(
                item["prefix"] == record["prefix"] for item in page["items"]
            )
            cursor = page["next_cursor"]
            if found or cursor is None:
                break
        require(found, "Storage browser listing omits the new archive")

    async def request(self, http, path, source, *, multipart=False, **options):
        """Submit a timed OCR request using JSON or multipart document input."""
        start = time.monotonic()
        if multipart:
            data = {
                k: json.dumps(v) if isinstance(v, (dict, bool)) else str(v)
                for k, v in options.items()
            }
            response = await http.post(
                path,
                files={"file": ("calibration.png", source, "image/png")},
                data=data,
            )
        else:
            response = await http.post(
                path,
                json={"image_base64": base64.b64encode(source).decode(), **options},
            )
        return response, round((time.monotonic() - start) * 1000, 2)

    async def ocr(self, figure):
        """Wait for readiness and verify OCR archival and browser access."""
        async with httpx.AsyncClient(
            base_url=self.api, headers=self.headers, timeout=660
        ) as http:
            # Full autotuning can use 20 minutes before Uvicorn starts.
            for attempt in range(360):
                try:
                    ready = await http.get("/health", timeout=5)
                    if (
                        ready.status_code == 200
                        and ready.json().get("status") == "healthy"
                    ):
                        break
                except (httpx.HTTPError, ValueError):
                    pass
                if attempt % 6 == 0:
                    print("Waiting for API startup/calibration", flush=True)
                await asyncio.sleep(5)
            health = await http.get("/health")
            require(
                health.status_code == 200 and health.json()["status"] == "healthy",
                "API not ready",
            )
            before = await http.get("/metrics")
            for path, multipart, options in [
                ("/parse", False, {"include_images": False, "json_rag": True}),
                ("/parse", True, {"include_images": False}),
                ("/ocr", False, {}),
                ("/parse/chunks", False, {"options": {"max_tokens": 512}}),
            ]:
                response, elapsed = await self.request(
                    http, path, self.plain, multipart=multipart, **options
                )
                record = self.check_archive(response, self.plain)
                await self.check_browser_archive(http, record)
                record.update(http_ms=elapsed, multipart=multipart)
                require(
                    "quick brown fox"
                    in self.get(
                        record["manifest_key"].replace("manifest.json", "document.md")
                    )
                    .decode()
                    .lower(),
                    "Known OCR text absent",
                )
                print("PASS", path, "multipart" if multipart else "json", flush=True)
            source = figure.read_bytes() if figure else synthetic_figure()
            response, elapsed = await self.request(
                http, "/parse", source, include_images=False
            )
            record = self.check_archive(response, source, require_images=True)
            await self.check_browser_archive(http, record)
            record.update(http_ms=elapsed, fixture="figure")
            print("PASS hidden figure PNG", flush=True)
            responses = await asyncio.gather(
                *[self.request(http, "/ocr", self.plain) for _ in range(2)]
            )
            for response, elapsed in responses:
                record = self.check_archive(response, self.plain)
                await self.check_browser_archive(http, record)
                record.update(http_ms=elapsed, concurrent=True)
            after = await http.get("/metrics")
        return {
            "archives": list(self.records),
            "storage_metrics": metric_delta(before.text, after.text),
        }

    async def mcp(self):
        """Verify MCP archive workflows and their effects on stored manifests."""
        import httpx2
        from mcp.client import Client
        from mcp.client.streamable_http import streamable_http_client
        from ocr_mcp.config import parse_api_tokens

        tokens = parse_api_tokens(self.env["MCP_API_TOKENS"])
        before = self.manifests()
        url = f"http://127.0.0.1:{self.env.get('MCP_PORT', '8021')}/mcp"
        async with (
            httpx2.AsyncClient(
                headers={"Authorization": "Bearer " + next(iter(tokens))}, timeout=660
            ) as http,
            Client(
                streamable_http_client(url, http_client=http), read_timeout_seconds=660
            ) as mcp,
        ):
            result = await mcp.call_tool(
                "ocr_parse_document",
                {
                    "params": {
                        "image_base64": base64.b64encode(self.plain).decode(),
                        "filename": "mcp-source.png",
                        "response_format": "json",
                    }
                },
            )
            require(not result.is_error, "MCP OCR failed")
            require(bool(result.structured_content["pages"]), "MCP result missing")
            chunks = await mcp.call_tool(
                "ocr_create_rag_chunks",
                {
                    "params": {
                        "image_base64": base64.b64encode(self.plain).decode(),
                        "filename": "mcp-chunks.png",
                        "options": {"max_tokens": 512},
                        "response_format": "json",
                    }
                },
            )
            require(not chunks.is_error, "MCP chunk creation failed")
            require(bool(chunks.structured_content["chunks"]), "MCP chunks missing")
        created = self.manifests() - before
        require(len(created) == 2, "MCP did not create both requested archives")
        archives = {}
        for key in created:
            manifest = json.loads(self.get(key))
            kind = manifest["kind"]
            expected_name = (
                "mcp-chunks.png" if kind == "parse-chunks" else "mcp-source.png"
            )
            require(
                manifest["source"]["sha256"] == hashlib.sha256(self.plain).hexdigest(),
                "MCP source differs",
            )
            require(
                manifest["source"].get("original_name") == expected_name
                and key.removesuffix("manifest.json").startswith(expected_name + "__"),
                "MCP source filename did not reach the archive",
            )
            archives[kind] = key
        require(
            set(archives) == {"parse", "parse-chunks"},
            "MCP archive kinds differ",
        )
        return {"created_archives": archives, "response_contract_preserved": True}

    def manifests(self):
        """Return all current manifest keys through the read-only S3 role."""
        return {
            item["Key"]
            for page in self.reader.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket
            )
            for item in page.get("Contents", [])
            if item["Key"].endswith("/manifest.json")
        }

    async def faults(self):
        """Stop storage temporarily and verify API failure isolation and recovery."""
        existing = sorted(self.manifests())
        require(bool(existing), "Run OCR stage before faults")
        preserved = self.get(existing[0])
        preserved_files = {
            item["key"]: self.get(item["key"])
            for item in json.loads(preserved)["artifacts"]
        }
        failures = []
        docker("stop", "seaweedfs")
        try:
            async with httpx.AsyncClient(
                base_url=self.api, headers=self.headers, timeout=660
            ) as http:
                for path in ("/ocr", "/parse", "/parse/chunks"):
                    options = (
                        {"options": {"max_tokens": 512}}
                        if path.endswith("chunks")
                        else {}
                    )
                    response, elapsed = await self.request(
                        http, path, self.plain, **options
                    )
                    require(response.status_code == 200, "Storage outage lost OCR")
                    require(
                        response.headers.get("x-ocr-storage-status") == "failed",
                        "Outage not signaled",
                    )
                    require(
                        "x-ocr-archive-key" not in response.headers,
                        "False archive confirmation",
                    )
                    payload = response.json()
                    if path.endswith("chunks"):
                        require(
                            set(payload) == {"chunks"} and payload["chunks"],
                            "Outage changed RAG contract",
                        )
                    else:
                        require(
                            any(
                                w["code"] == "storage_failed"
                                for w in payload["warnings"]
                            ),
                            "Warning missing",
                        )
                        text = payload.get("result") or "\n".join(
                            page.get("markdown") or ""
                            for page in payload.get("pages", [])
                        )
                        require(
                            "quick brown fox" in text.lower(), "Outage OCR content lost"
                        )
                    failures.append(
                        {
                            "endpoint": path,
                            "http_ms": elapsed,
                            "http_status": 200,
                            "storage_status": "failed",
                        }
                    )
                start = time.monotonic()
                health = await http.get("/health")
                require(
                    health.status_code == 200 and health.json()["status"] == "healthy",
                    "Health depends on storage",
                )
                health_ms = round((time.monotonic() - start) * 1000, 2)
        finally:
            docker("start", "seaweedfs")
        await self.wait_storage()
        require(self.get(existing[0]) == preserved, "Restart lost archive")
        for key, blob in preserved_files.items():
            require(self.get(key) == blob, "Restart lost artifact")
        async with httpx.AsyncClient(
            base_url=self.api, headers=self.headers, timeout=660
        ) as http:
            response, elapsed = await self.request(http, "/ocr", self.plain)
            recovery = self.check_archive(response, self.plain)
            health = await http.get("/health")
            require(
                health.json()["storage"]["status"] == "ok", "Diagnostics not recovered"
            )
        return {
            "outage": failures,
            "health_ms_during_outage": health_ms,
            "persisted_manifest": existing[0],
            "recovery": recovery,
        }

    async def wait_storage(self):
        """Poll S3 readiness for at most sixty attempts after storage restarts."""
        for _ in range(60):
            try:
                self.admin.list_buckets()
                return
            except (BotoCoreError, ClientError):
                await asyncio.sleep(1)
        raise RuntimeError("Storage did not recover")

    def lifecycle(self):
        """Verify selective object expiration in a temporary bucket and clean it up."""
        bucket = "ocr-expiration-check-" + str(int(time.time()))
        self.admin.create_bucket(Bucket=bucket)
        try:
            for key in ("expire/result.json", "keep/result.json"):
                self.admin.put_object(Bucket=bucket, Key=key, Body=b"{}")
            past = (datetime.now(timezone.utc) - timedelta(days=2)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            self.admin.put_bucket_lifecycle_configuration(
                Bucket=bucket,
                LifecycleConfiguration={
                    "Rules": [
                        {
                            "ID": "verification-only",
                            "Status": "Enabled",
                            "Filter": {"Prefix": "expire/"},
                            "Expiration": {"Date": past},
                        }
                    ]
                },
            )
            docker(
                "exec",
                "-T",
                "seaweedfs",
                "weed",
                "shell",
                "-master=127.0.0.1:9333",
                "-filer=127.0.0.1:8888",
                input_text=(
                    "s3.lifecycle.run-shard -shards 0-15 -s3 127.0.0.1:18333 "
                    "-refresh 1s -runtime 10s\n"
                ),
                timeout=35,
            )
            try:
                self.admin.head_object(Bucket=bucket, Key="expire/result.json")
            except ClientError as error:
                require(
                    error.response["Error"]["Code"] in ("404", "NoSuchKey"),
                    "Unexpected expiration error",
                )
            else:
                raise RuntimeError("Expired object remains")
            require(
                self.admin.head_object(Bucket=bucket, Key="keep/result.json")[
                    "ContentLength"
                ]
                == 2,
                "Lifecycle removed excluded object",
            )
            return {
                "expired_object": "404",
                "excluded_object_preserved": True,
                "engine": "same daily-run engine, manually invoked",
                "production_retention_unchanged": True,
            }
        finally:
            for key in ("expire/result.json", "keep/result.json"):
                self.admin.delete_object(Bucket=bucket, Key=key)
            # SeaweedFS preserves empty prefix directories; strict bucket-delete
            # mode requires removing them explicitly, even with no listed keys.
            for key in ("expire/", "keep/"):
                self.admin.delete_object(Bucket=bucket, Key=key)
            self.admin.delete_bucket(Bucket=bucket)


def synthetic_figure():
    """Render a synthetic document image for archive figure-capture checks."""
    image = Image.new("RGB", (1200, 1500), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(ROOT / "calibration/fonts/DejaVuSans.ttf"), 34)
    draw.text((90, 90), "STORAGE VALIDATION REPORT", fill="black", font=font)
    draw.text(
        (90, 180),
        "The landscape below is a figure for archive testing.",
        fill="black",
        font=font,
    )
    draw.rectangle((130, 330, 1070, 1040), fill="#a5d8ff")
    draw.ellipse((800, 380, 950, 530), fill="#ffd43b")
    draw.polygon([(130, 1000), (380, 480), (640, 1000)], fill="#405c75")
    draw.polygon([(420, 1040), (750, 570), (1070, 1040)], fill="#56738a")
    draw.polygon([(330, 585), (380, 480), (440, 600)], fill="white")
    draw.rectangle((130, 1000, 1070, 1040), fill="#4d8046")
    draw.text((130, 1100), "Figure 1: mountains, sky and sun.", fill="black", font=font)
    draw.text(
        (90, 1260), "The original file is not retained in S3.", fill="black", font=font
    )
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def metric_delta(before, after):
    """Return observed storage timing and byte-counter differences."""

    def values(text):
        return {
            line.split()[0]: float(line.split()[1])
            for line in text.splitlines()
            if line.startswith(
                (
                    "ocr_storage_duration_seconds_sum ",
                    "ocr_storage_duration_seconds_count ",
                    "ocr_storage_bytes_total ",
                )
            )
        }

    old, new = values(before), values(after)
    return {key: new[key] - old.get(key, 0) for key in new}


async def main():
    """Dispatch selected live stages and save synthetic verification evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage",
        choices=["all", "infrastructure", "ocr", "mcp", "faults", "lifecycle"],
        default="all",
    )
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--figure", type=Path)
    parser.add_argument(
        "--report-dir", type=Path, default=ROOT / "docs/verification/seaweedfs"
    )
    args = parser.parse_args()
    verification = Verification(args.env_file)
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stage": args.stage,
        "passed": False,
    }
    try:
        for stage in ["infrastructure", "ocr", "mcp", "faults", "lifecycle"]:
            if args.stage not in ("all", stage):
                continue
            if stage in ("infrastructure", "lifecycle"):
                result = getattr(verification, stage)()
            elif stage == "ocr":
                result = await verification.ocr(args.figure)
            else:
                result = await getattr(verification, stage)()
            report[stage] = result
            print("PASS", stage, flush=True)
        report["passed"] = True
    except Exception as error:  # noqa: BLE001 - sanitize every verification failure
        # No SDK/HTTP exception messages, requests or environment values in logs.
        report["error_type"] = type(error).__name__
        if isinstance(error, ClientError):
            report["error_code"] = error.response.get("Error", {}).get("Code")
            report["operation"] = error.operation_name
        report["error_location"] = [
            {"file": Path(frame.filename).name, "line": frame.lineno}
            for frame in traceback.extract_tb(error.__traceback__)[-4:]
        ]
        if isinstance(error, RuntimeError):
            report["check"] = str(error)
        print("FAIL", report.get("check", report["error_type"]), flush=True)
    finally:
        for actor in (
            verification.admin,
            verification.writer,
            verification.reader,
            verification.browser,
        ):
            actor.close()
        args.report_dir.mkdir(parents=True, exist_ok=True)
        (args.report_dir / f"{args.stage}.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    asyncio.run(main())
