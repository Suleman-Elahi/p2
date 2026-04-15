#!/usr/bin/env python
"""p2 manage.py"""
import os
import sys
from pathlib import Path

if __name__ == "__main__":
    # Load .env file before Django settings
    from dotenv import load_dotenv
    env_path = Path(__file__).parent / ".env"
    load_dotenv(env_path)

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "p2.core.settings")
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
