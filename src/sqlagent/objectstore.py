"""Neon Object Storage: where uploaded files are kept.

S3-compatible, so boto3 is used. Neon requires path-style addressing and SigV4.

The credentials are passed to the client explicitly rather than left to the
default AWS credential chain. Neon injects ``AWS_ACCESS_KEY_ID`` and friends,
and anything else in this process that relies on the chain (Bedrock) would
otherwise start signing with a storage credential.

Unset endpoint means no object store: uploads stay on local disk only, which is
the right behaviour on a laptop.
"""

from __future__ import annotations

import logging
from pathlib import Path

import boto3
from botocore.config import Config

from sqlagent.config import Settings

logger = logging.getLogger(__name__)


class ObjectStoreError(Exception):
    """The upload could not be stored."""


class ObjectStore:
    def __init__(self, config: Settings, client=None) -> None:
        self.bucket = config.object_store_bucket
        self.client = client or boto3.client(
            "s3",
            endpoint_url=config.object_store_endpoint,
            region_name=config.object_store_region or None,
            aws_access_key_id=config.object_store_access_key,
            aws_secret_access_key=config.object_store_secret_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    @staticmethod
    def key(dataset_id: str, name: str) -> str:
        # Only the final path component: an uploaded name is attacker-controlled.
        return f"datasets/{dataset_id}/{Path(name).name or 'upload'}"

    def put(self, dataset_id: str, name: str, source: Path) -> str:
        key = self.key(dataset_id, name)
        try:
            self.client.upload_file(str(source), self.bucket, key)
        except Exception as exc:  # noqa: BLE001
            logger.exception("object store upload failed for %s", key)
            raise ObjectStoreError("Could not store the uploaded file.") from exc
        return key

    def delete_dataset(self, dataset_id: str) -> int:
        """Remove every object stored for a dataset. Returns how many."""
        prefix = f"datasets/{dataset_id}/"
        listing = self.client.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
        keys = [{"Key": item["Key"]} for item in listing.get("Contents", [])]
        if keys:
            self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})
        return len(keys)


def object_store(config: Settings) -> ObjectStore | None:
    """The configured store, or ``None`` when no endpoint is set."""
    if not config.object_store_endpoint:
        return None
    return ObjectStore(config)
