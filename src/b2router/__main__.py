"""``python -m b2router`` entrypoint."""

from __future__ import annotations

import sys

from b2router.server import main

if __name__ == "__main__":
    sys.exit(main())
