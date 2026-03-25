#!/usr/bin/env python
"""p2 manage.py"""
import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "p2.core.settings")
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
