"""Run the Django ASGI application with Uvicorn on DJANGO_HOST:DJANGO_PORT.

Reads ``DJANGO_HOST`` and ``DJANGO_PORT`` (loaded from ``.env`` in settings),
so the bound address stays configurable without duplicating env parsing.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import uvicorn
from django.conf import settings


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serve the application on DJANGO_HOST:DJANGO_PORT.",
    )
    parser.add_argument(
        "--noreload",
        action="store_true",
        help="Disable the autoreloader.",
    )
    args = parser.parse_args()

    uvicorn.run(
        "config.asgi:application",
        host=settings.DJANGO_HOST,
        port=settings.DJANGO_PORT,
        reload=not args.noreload,
        lifespan="on",
    )


if __name__ == "__main__":
    main()
