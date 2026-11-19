"""Worker settings, read from the environment."""

import os
import socket
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    database_url: str
    s3_bucket: str
    s3_region: str
    # Set only for an S3-compatible local store. Unset on AWS.
    s3_endpoint: str | None
    # Unset on AWS so boto3 uses its default credential chain.
    s3_access_key_id: str | None
    s3_secret_access_key: str | None
    batfish_host: str
    # Name recorded against parsed models, so it is known which instance holds them.
    batfish_instance: str
    worker_id: str
    poll_seconds: float
    lease_seconds: int
    # Model access for the agent. Without a key, agent replies report that it is not configured.
    deepseek_api_key: str | None
    agent_model: str
    # Embeddings for the documentation index. Without a key, the agent has no search_docs tool.
    openai_api_key: str | None
    embedding_model: str
    # The Containerlab lab, used by the deploy worker only.
    containerlab: str
    lab_kind: str
    # The router image, e.g. vrnetlab/cisco_iol:17.12.01. Unset means deployments fail with a message saying so.
    lab_image: str | None
    lab_username: str
    lab_password: str
    lab_dir: str
    lab_converge_seconds: int
    lab_mgmt_subnet: str


def load() -> Config:
    env = os.environ
    batfish_host = env.get("BATFISH_HOST", "127.0.0.1")
    return Config(
        database_url=env["DATABASE_URL"],
        s3_bucket=env["S3_BUCKET"],
        s3_region=env["S3_REGION"],
        s3_endpoint=env.get("S3_ENDPOINT") or None,
        s3_access_key_id=env.get("S3_ACCESS_KEY_ID") or None,
        s3_secret_access_key=env.get("S3_SECRET_ACCESS_KEY") or None,
        batfish_host=batfish_host,
        batfish_instance=env.get("BATFISH_INSTANCE") or batfish_host,
        worker_id=env.get("WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}",
        poll_seconds=float(env.get("WORKER_POLL_SECONDS", "1")),
        lease_seconds=int(env.get("WORKER_LEASE_SECONDS", "60")),
        deepseek_api_key=env.get("DEEPSEEK_API_KEY") or None,
        agent_model=env.get("AGENT_MODEL") or "deepseek-flash",
        openai_api_key=env.get("OPENAI_API_KEY") or None,
        embedding_model=env.get("EMBEDDING_MODEL") or "text-embedding-3-small",
        containerlab=env.get("CONTAINERLAB") or "containerlab",
        lab_kind=env.get("LAB_KIND") or "cisco_iol",
        lab_image=env.get("LAB_IMAGE") or None,
        lab_username=env.get("LAB_USERNAME") or "admin",
        lab_password=env.get("LAB_PASSWORD") or "admin",
        lab_dir=env.get("LAB_DIR") or os.path.expanduser("~/.local/share/verifier/labs"),
        lab_converge_seconds=int(env.get("LAB_CONVERGE_SECONDS", "300")),
        lab_mgmt_subnet=env.get("LAB_MGMT_SUBNET") or "172.31.255.0/24",
    )
