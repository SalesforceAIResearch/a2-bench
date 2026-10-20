"""``python -m a2_run ...`` dispatches to :func:`a2_run.cli.main`."""
from .cli import main
import sys

if __name__ == "__main__":
    sys.exit(main())
