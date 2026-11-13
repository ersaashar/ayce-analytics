# AYCE Analytics V2 — task runner.
#
# Requires `just` (https://github.com/casey/just), `uv` and Node.js 20.19+/22.12+.
# All Python commands run through uv so the locked environment is used.

set dotenv-load := false

# npm's extensionless launcher is broken under Git Bash on Windows.
npm := if os() == "windows" { "npm.cmd" } else { "npm" }

# List available recipes.
default:
    @just --list

# Install dependencies exactly as locked.
install:
    uv sync --locked
    {{npm}} ci

# Apply database migrations.
migrate:
    uv run --locked python manage.py migrate

# First-time setup: install dependencies and migrate.
setup: install migrate

# Build the chat frontend bundle into chat/static/chat/dist.
frontend-build:
    {{npm}} run build

# Lint and typecheck the frontend without bundling.
frontend-check:
    {{npm}} run lint
    {{npm}} run typecheck

# Run the frontend protocol tests.
frontend-test:
    {{npm}} test

# Run the development server on DJANGO_HOST:DJANGO_PORT from .env.
dev: frontend-build
    uv run --locked python scripts/serve.py

# Run the development server without the autoreloader.
dev-noreload: frontend-build
    uv run --locked python scripts/serve.py --noreload

# Print the resolved host and port from .env.
bind:
    @uv run --locked python -c "import os,sys;sys.path.insert(0,'.');os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings');from django.conf import settings;print(f'{settings.DJANGO_HOST}:{settings.DJANGO_PORT}')"

# Run Django's system checks.
check:
    uv run --locked python manage.py check

# Run the full test suite.
test:
    uv run --locked python manage.py test tests --verbosity 2

# Lint without modifying files.
lint:
    uv run --locked ruff check .

# Check formatting without modifying files.
lint-format:
    uv run --locked ruff format --check .

# Apply lint fixes and formatting.
fmt:
    uv run --locked ruff check --fix .
    uv run --locked ruff format .

# Run all pre-commit hooks against every file.
hooks:
    uv run --locked pre-commit run --all-files

# Install the git pre-commit hook.
hooks-install:
    uv run --locked pre-commit install

# Reproduce the CI pipeline locally.
ci: lint lint-format check frontend-check frontend-test frontend-build test

# Show ruff's version and resolved configuration.
info:
    @uv run --locked ruff --version
    @uv run --locked python -c "import sys,django,pydantic_ai;print('python',sys.version.split()[0]);print('django',django.get_version());print('pydantic-ai',pydantic_ai.__version__)"
