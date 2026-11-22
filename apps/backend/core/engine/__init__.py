"""The engine contract: what Lightbulb Phone and Meetings call (`contract`, and the operations through `engine`) and
what the engine calls back (`hooks`). Products import only this package from the engine.

`engine` is the operations' module for this process's transport (`settings.ENGINE_TRANSPORT`): `api` in process,
`client` over HTTP. Both have the same functions and signatures. Resolved on first use, so importing the contract
never imports the operations.
"""


def __getattr__(name: str):
    if name != "engine":
        raise AttributeError(f"module 'core.engine' has no attribute {name!r}")
    from core.config import settings

    if settings.ENGINE_TRANSPORT == "http":
        from core.engine import client as engine
    else:
        from core.engine import api as engine
    globals()["engine"] = engine
    return engine
