"""Start the distributed stack and open its verified gateway."""

import sys
from distributed.scripts.start import entrypoint

if __name__ == "__main__":
    entrypoint(["--open", *sys.argv[1:]])
