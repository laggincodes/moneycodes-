"""
WebsiteLead Hunter - Windows CLI Entry Point.
Usage:
    python cli.py --help
    python cli.py doctor
    python cli.py login
    python cli.py leads list
"""
import os
import sys

# Ensure backend directory is in sys.path
backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
