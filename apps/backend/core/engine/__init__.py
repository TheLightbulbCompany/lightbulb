"""The engine contract: what Lightbulb Phone and Meetings call (`client`, the operations over HTTP, with `contract`'s
models) and what the engine calls back (`hooks`). Products import only `client`, `contract`, `hooks` and `transport`
from here; `api` is the engine's own side.
"""
