# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""`python -m crux` — delegates to the CLI."""
import sys

from crux.cli import main

if __name__ == "__main__":
    sys.exit(main())
