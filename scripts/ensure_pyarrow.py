"""Ensure pyarrow is installed for the current interpreter."""
from __future__ import annotations

import subprocess
import sys


def main() -> int:
    try:
        import pyarrow  # noqa: F401

        print(f"OK pyarrow already installed: {pyarrow.__version__} ({sys.executable})")
        return 0
    except Exception:
        pass

    print(f"Installing pyarrow for {sys.executable} ...")
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "pyarrow>=14.0.0"],
        check=False,
    )
    if r.returncode != 0:
        print("FAILED: pip install pyarrow")
        return r.returncode
    import pyarrow

    print(f"OK pyarrow {pyarrow.__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
