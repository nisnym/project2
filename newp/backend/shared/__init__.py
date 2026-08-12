"""Shared kernel for the PFA microservices.

Everything in here is transport/plumbing or contract — no business rules.
Business rules live inside the service that owns the domain, so a judge can
ask "where does this number come from?" and land in exactly one file.
"""
