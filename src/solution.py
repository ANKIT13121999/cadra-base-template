"""F1 capstone solution entry point."""
from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("src.api:app", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
