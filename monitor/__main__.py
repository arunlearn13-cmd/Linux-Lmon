import os

import uvicorn


def main():
    host = os.getenv("MONITOR_HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1") and not os.getenv("MONITOR_TOKEN"):
        print("WARNING: listening on a non-loopback address without MONITOR_TOKEN; "
              "put it behind an authenticating reverse proxy.")
    uvicorn.run("monitor.server:app", host=host, port=int(os.getenv("MONITOR_PORT", "8202")))


if __name__ == "__main__":
    main()
