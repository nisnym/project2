#!/usr/bin/env python
"""Django entrypoint for kyc-svc."""
import os
import sys
from pathlib import Path

# The service directory itself must be importable so `config` and `kyc` resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
