"""Entry point for the PyInstaller build: imports the package properly, so relative imports work."""
import sys

from dwmanager.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
