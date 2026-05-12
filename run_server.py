#!/usr/bin/env python3
"""Start the FinOps Agent server."""

import os
import sys

import uvicorn

sys.path.insert(0, os.path.dirname(__file__))

if __name__ == "__main__":
    uvicorn.run(
        "backend.server.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=False,
        log_level="info",
    )
