"""Run inside the actual Linux ARM64 backend image; no network or provider calls."""

import importlib
import json
import os
import platform
import secrets
import subprocess
import sys

assert platform.system() == "Linux", platform.system()
assert platform.machine() == "aarch64", platform.machine()
sys.path.insert(0, "/app")
from cryptography.fernet import Fernet  # noqa: E402

os.environ.update({
    "CLERK_ISSUER": "https://arm64-smoke.invalid",
    "ENCRYPTION_KEY": Fernet.generate_key().decode(),
    "NETWORK_PROOF_KEY": secrets.token_urlsafe(32),
    "GATE_BEARER_HMAC_KEY": secrets.token_urlsafe(32),
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_REGION": "us-east-1",
    "AWS_EC2_METADATA_DISABLED": "true",
    "AWS_ENDPOINT_URL": "http://127.0.0.1:9",
    "AWS_MAX_ATTEMPTS": "1",
})
modules = [
    "pydantic_core._pydantic_core",
    "cryptography.hazmat.bindings._rust",
    "aiohttp._http_parser",
    "temporalio.bridge.runtime",
    "awslambdaric.bootstrap",
    "main",
    "scripts.temporal_worker",
    "scripts.provision_lambda",
    "scripts.work_lambda",
]
for name in modules:
    importlib.import_module(name)
subprocess.run(["gog", "--version"], check=True)
print(json.dumps({"platform": platform.system(), "machine": platform.machine(), "importsPassed": modules, "gogPassed": True}))
