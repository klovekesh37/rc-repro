"""Compatibility shim.

All real configuration lives in pyproject.toml. This file exists only so that
`pip install -e .` also works on older pip (< 21.3), which can't do editable
installs from pyproject.toml alone (PEP 660). Modern pip ignores it and uses
pyproject.toml.
"""

from setuptools import setup

setup()
