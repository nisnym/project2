"""Shared library for every service in the banking platform.

Everything cross-cutting lives here exactly once: the event backbone, auth,
service-to-service HTTP, observability, and the Money value object. Ten Django
projects that each reinvent these would drift into ten sets of conventions.
"""

__version__ = "0.1.0"
