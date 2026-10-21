"""Shared test setup. Runs before any test module imports config."""

import os

# config.py loads the repo-root .env on import; a developer's local .env
# (e.g. MODEL_PROVIDER=vertex) must not change test results.
os.environ["FFAV_SKIP_DOTENV"] = "1"
