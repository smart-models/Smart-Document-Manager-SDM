"""Credential rotation must validate the replacement before revoking access."""

import json
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from storage import bootstrap


class IAM:
    """Emulate SeaweedFS IAM keys and policy updates for bootstrap tests."""

    def __init__(self):
        self.keys = {"existing-key": "existing-secret"}
        self.policy = bootstrap.policies("ocr-outputs")[0]
        self.versions_written = 0
        self.meta = SimpleNamespace(endpoint_url="http://storage", events=self)

    def create_user(self, **kwargs):
        raise ClientError({"Error": {"Code": "EntityAlreadyExists"}}, "CreateUser")

    def list_access_keys(self, **kwargs):
        return {"AccessKeyMetadata": [{"AccessKeyId": key} for key in self.keys]}

    def register(self, event, callback, **kwargs):
        self.callback = callback

    def unregister(self, *args, **kwargs):
        self.callback = None

    def create_access_key(self, **kwargs):
        params = {"body": {}}
        self.callback(params)
        body = params["body"]
        self.keys[body["AccessKeyId"]] = body["SecretAccessKey"]

    def get_policy(self, **kwargs):
        return {"Policy": {"DefaultVersionId": "v1"}}

    def get_policy_version(self, **kwargs):
        return {"PolicyVersion": {"Document": self.policy}}

    def create_policy_version(self, **kwargs):
        self.versions_written += 1
        self.policy = json.loads(kwargs["PolicyDocument"])

    def attach_user_policy(self, **kwargs):
        pass

    def delete_access_key(self, **kwargs):
        del self.keys[kwargs["AccessKeyId"]]


@pytest.fixture
def iam(monkeypatch):
    iam = IAM()

    def make_client(service, endpoint, access, secret):
        class Probe:
            def list_buckets(self):
                if iam.keys.get(access) != secret:
                    raise ClientError(
                        {"Error": {"Code": "SignatureDoesNotMatch"}}, "ListBuckets"
                    )
                return {}

            def close(self):
                pass

        return Probe()

    monkeypatch.setattr(bootstrap, "client", make_client)
    return iam


def test_repeat_initialization_preserves_keys_and_policy(iam):
    for _ in range(2):
        bootstrap.ensure_user(
            iam, "ocr-writer", "existing-key", "existing-secret", iam.policy
        )
    assert iam.keys == {"existing-key": "existing-secret"}
    assert iam.versions_written == 0


def test_secret_only_change_fails_without_revoking_working_key(iam):
    with pytest.raises(RuntimeError, match="Rotate both"):
        bootstrap.ensure_user(
            iam, "ocr-writer", "existing-key", "incorrect-secret", iam.policy
        )
    assert iam.keys == {"existing-key": "existing-secret"}


def test_rotation_validates_new_key_then_revokes_previous_key(iam):
    bootstrap.ensure_user(
        iam, "ocr-writer", "replacement-key", "replacement-secret", iam.policy
    )
    assert iam.keys == {"replacement-key": "replacement-secret"}


def test_changed_policy_reconciles_once(iam):
    policy = bootstrap.policies("different-ocr-bucket")[0]
    for _ in range(2):
        bootstrap.ensure_user(
            iam, "ocr-writer", "existing-key", "existing-secret", policy
        )
    assert iam.policy == policy
    assert iam.versions_written == 1
