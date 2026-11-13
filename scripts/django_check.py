"""Run Django's system check with placeholder settings.

Settings require ``DJANGO_SECRET_KEY``; supply dummy values so the check works
without a local ``.env`` file. Existing environment values are never overridden.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("DJANGO_SECRET_KEY", "check-only-placeholder-secret-key-0123")
os.environ.setdefault("DJANGO_DEBUG", "false")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver")
for empty in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
    os.environ.setdefault(empty, "")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if __name__ == "__main__":
    from django.core.management import execute_from_command_line

    execute_from_command_line(["manage.py", "check", *sys.argv[1:]])
