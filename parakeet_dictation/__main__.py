"""`python -m parakeet_dictation` — the same entry point as the console script."""

import sys

from .app import main

sys.exit(main())
