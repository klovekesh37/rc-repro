"""rc-repro — version-matched Rocket.Chat reproduction environments."""

from importlib import metadata

try:
    # Single source of truth: pyproject.toml's [project] version.
    __version__ = metadata.version("rc-repro")
except metadata.PackageNotFoundError:  # running from a raw checkout
    __version__ = "0.0.0-dev"
