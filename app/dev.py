from __future__ import annotations

import argparse
from importlib.util import find_spec
import os


REQUIRED_MODULES = ("alembic", "fastapi", "sqlalchemy", "uvicorn")


def _require_dependencies() -> None:
    missing = [module for module in REQUIRED_MODULES if find_spec(module) is None]
    if missing:
        names = ", ".join(missing)
        raise SystemExit(
            f"Missing Sightline dependencies: {names}. "
            "Install this checkout with: python -m pip install -e ."
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Sightline directly from a source checkout")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--log-level", default="info")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _require_dependencies()
    os.environ["SIGHTLINE_SOURCE_SERVER"] = "1"

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
