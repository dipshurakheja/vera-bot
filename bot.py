"""magicpin AI Challenge — Vera message engine entry point.

    uvicorn bot:app --host 0.0.0.0 --port 8080      # the HTTP bot the judge talks to
    from bot import compose                           # the pure composition function (brief §7.1)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _load_dotenv(path: Path = Path(__file__).with_name(".env")) -> None:
    """Local convenience: read .env if present. Real environment variables (e.g. on Render) always win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            if value.strip() and key.strip() not in os.environ:
                os.environ[key.strip()] = value.strip()


_load_dotenv()

from vera.api.app import create_app  # noqa: E402
from vera.composer.engine import compose as _compose  # noqa: E402
from vera.composer.engine import public_view  # noqa: E402


def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None,
            now: Optional[str] = None) -> dict:
    """Deterministic compose(category, merchant, trigger, customer?) -> body/cta/send_as/suppression_key/rationale.

    Same inputs (and same optional `now`) always return the same output.
    """
    return public_view(_compose(category, merchant, trigger, customer, now=now))


app = create_app()

if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("bot:app", host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")))
