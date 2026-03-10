"""
Secret loading - supports .env file and AWS Secrets Manager.
Reads AGENT_PRIVATE_KEY and IFTTT_WEBHOOK_KEY.

Usage:
    Local:  secrets loaded from .env file (default)
    AWS:    SECRET_SOURCE=aws SECRET_NAME=boros/bot-keys python main.py
"""
import os
import json
import logging

logger = logging.getLogger(__name__)


def load_secrets():
    """Load secrets into environment variables from the configured source."""
    source = os.environ.get("SECRET_SOURCE", "env")

    if source == "aws":
        _load_from_aws()
    elif source == "gcp":
        _load_from_gcp()
    else:
        _load_from_dotenv()


def _load_from_dotenv():
    """Load from .env file in project root."""
    env_path = os.path.join(os.path.dirname(__file__), '..', '..', '.env')
    env_path = os.path.abspath(env_path)
    if not os.path.exists(env_path):
        logger.info("No .env file found, using existing environment")
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip())
    logger.info("Secrets loaded from .env")


def _load_from_aws():
    """Load from AWS Secrets Manager. Expects JSON: {"AGENT_PRIVATE_KEY": "...", ...}"""
    import boto3
    secret_name = os.environ.get("SECRET_NAME", "boros/bot-keys")
    region = os.environ.get("AWS_REGION", "us-west-2")

    client = boto3.client("secretsmanager", region_name=region)
    response = client.get_secret_value(SecretId=secret_name)
    secrets = json.loads(response["SecretString"])

    for key, value in secrets.items():
        os.environ.setdefault(key, value)
    logger.info("Secrets loaded from AWS Secrets Manager (%s)", secret_name)


def _load_from_gcp():
    """Load from GCP Secret Manager. Expects JSON: {"AGENT_PRIVATE_KEY": "...", ...}"""
    from google.cloud import secretmanager
    secret_name = os.environ.get("SECRET_NAME", "boros-bot-keys")
    project_id = os.environ.get("GCP_PROJECT_ID")

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
    response = client.access_secret_version(name=name)
    secrets = json.loads(response.payload.data.decode())

    for key, value in secrets.items():
        os.environ.setdefault(key, value)
    logger.info("Secrets loaded from GCP Secret Manager (%s)", secret_name)
