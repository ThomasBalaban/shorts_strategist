"""CLI entry. Currently just launches the API server."""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

if __name__ == "__main__":
    import uvicorn
    import config
    uvicorn.run("api_server:app", host="0.0.0.0", port=config.PORT, reload=False)
