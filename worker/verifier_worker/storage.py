"""Reads snapshot files from object storage."""

import hashlib
from pathlib import Path
from typing import Any

import boto3

from .config import Config


class IntegrityError(Exception):
    """A stored file no longer matches the hash recorded when it was uploaded."""


def make_client(config: Config):
    kwargs: dict[str, Any] = {"region_name": config.s3_region}
    if config.s3_endpoint:
        kwargs["endpoint_url"] = config.s3_endpoint
    if config.s3_access_key_id and config.s3_secret_access_key:
        kwargs["aws_access_key_id"] = config.s3_access_key_id
        kwargs["aws_secret_access_key"] = config.s3_secret_access_key
    return boto3.client("s3", **kwargs)


def put_text(s3, bucket: str, key: str, text: str) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=text.encode("utf-8"), ContentType="text/plain; charset=utf-8")


def download_snapshot(s3, bucket: str, prefix: str, files: list[dict[str, Any]], dest: Path) -> Path:
    """Write the snapshot's files under dest/configs/ and verify each against its recorded hash.

    Returns dest, laid out the way Batfish expects a snapshot directory.
    """
    configs = (dest / "configs").resolve()
    for entry in files:
        target = (configs / entry["path"]).resolve()
        if not target.is_relative_to(configs):
            raise ValueError(f"unsafe path in snapshot manifest: {entry['path']!r}")

        with s3.get_object(Bucket=bucket, Key=f"{prefix}configs/{entry['path']}")["Body"] as body:
            data = body.read()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise IntegrityError(f"stored file {entry['path']} does not match its recorded hash")

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return dest
