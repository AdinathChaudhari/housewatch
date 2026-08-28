"""``python -m housewatch <cmd>`` entry point. All argument parsing and
command implementations live in ``cli.py`` — this file is deliberately just
the ``python -m`` shim so ``import housewatch.__main__`` and
``python -m housewatch`` behave identically.
"""

from __future__ import annotations

import sys

from housewatch.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
