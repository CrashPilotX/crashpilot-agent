"""Allow `python -m crashpilot` and give packagers (PyInstaller) a clean entry point."""

from crashpilot.main import app

if __name__ == "__main__":
    app()
