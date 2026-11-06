"""Vendor-neutral external data interfaces and offline fixtures."""

from .base import ProviderBundle
from .fake import make_demo_bundle

__all__ = ["ProviderBundle", "make_demo_bundle"]
