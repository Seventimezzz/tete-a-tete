"""Dump the protocol JSON Schema: `python -m app.protocol_schema <path>`."""

import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.protocol import ClientMessage, ServerMessage

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_TITLE = "Tete-a-tete signaling protocol"


class _Protocol(BaseModel):
    """Wrapper so that both unions land in `$defs` under their own names."""

    client: ClientMessage
    server: ServerMessage


def build_schema() -> dict[str, Any]:
    defs = _Protocol.model_json_schema(ref_template="#/$defs/{model}")["$defs"]
    return {"$schema": SCHEMA_DIALECT, "title": SCHEMA_TITLE, "$defs": defs}


def write_schema(path: Path) -> None:
    text = json.dumps(build_schema(), indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m app.protocol_schema <path>")
    write_schema(Path(sys.argv[1]))
