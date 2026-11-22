"""Central configuration for the IntelliQuery backend.

Every value is read from an environment variable. A local ``backend/.env``
file is loaded when it exists (``python-dotenv``), but plain environment
variables work too, which is what a shell session or a deployment uses.

No credential has a hard-coded default: if ``DB_USER``, ``DB_PASSWORD`` or
``DB_NAME`` is missing, :func:`require_database_config` raises a
``ConfigurationError`` whose message names the missing variables.
"""

import os

try:  # python-dotenv is optional; environment variables alone are enough
    from dotenv import load_dotenv

    load_dotenv(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    )
except ImportError:  # pragma: no cover - only when the package is absent
    pass


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or unusable."""


def _text(name, default=""):
    """Return a stripped environment value, or ``default`` when unset."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


# ---------------------------------------------------------
# MySQL
# ---------------------------------------------------------

# Credentials deliberately have no defaults. They are validated by
# require_database_config() when a connection is opened, so importing a module
# for a unit test does not require a configured database.
DB_USER = _text("DB_USER")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_HOST = _text("DB_HOST", "localhost")
DB_PORT = _text("DB_PORT", "3306")
DB_NAME = _text("DB_NAME")

REQUIRED_DATABASE_VARIABLES = ("DB_USER", "DB_PASSWORD", "DB_NAME")


# ---------------------------------------------------------
# ARGO NetCDF files
# ---------------------------------------------------------

# Folder holding the local NetCDF corpus used by the batch ingester.
ARGO_DATA_DIR = _text("ARGO_DATA_DIR", "argo_data")


# ---------------------------------------------------------
# ChromaDB
# ---------------------------------------------------------

CHROMA_PERSIST_DIR = _text("CHROMA_PERSIST_DIR", "chroma_db_storage_fresh")
CHROMA_COLLECTION_NAME = _text("CHROMA_COLLECTION_NAME", "argo_float_profiles")


def missing_database_variables():
    """Return the names of required database variables that are not set."""
    values = {
        "DB_USER": DB_USER,
        "DB_PASSWORD": DB_PASSWORD,
        "DB_NAME": DB_NAME,
    }
    return [name for name in REQUIRED_DATABASE_VARIABLES if not values[name]]


def require_database_config():
    """Raise a clear error when the database configuration is incomplete."""
    missing = missing_database_variables()
    if missing:
        raise ConfigurationError(
            "Missing required database configuration: "
            + ", ".join(missing)
            + ". Copy backend/.env.example to backend/.env and fill in your "
              "MySQL connection details, or set these environment variables "
              "before starting the backend."
        )
