"""Run inside the actual Linux ARM64 backend image; no network or provider calls."""

import importlib
import json
import os
import platform
import sys

assert platform.system() == "Linux", platform.system()
assert platform.machine() == "aarch64", platform.machine()
sys.path.insert(0, "/app")

os.environ.update({
    "CLERK_ISSUER": "https://arm64-smoke.invalid",
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
    "main",
]
for name in modules:
    importlib.import_module(name)
print(json.dumps({"platform": platform.system(), "machine": platform.machine(), "importsPassed": modules}))
