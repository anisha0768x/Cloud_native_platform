import importlib
import os
import uvicorn

service = os.environ["HELIO_SERVICE"]
app = importlib.import_module("helio.services." + service).app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, proxy_headers=False)
