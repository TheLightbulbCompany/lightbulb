"""Billing helpers package."""

import importlib

__all__ = ["bedrock_gate_url"]


def __getattr__(name: str):
    if name == "bedrock_gate_url":
        return importlib.import_module("core.billing.bedrock_gate_url")
    raise AttributeError(name)
