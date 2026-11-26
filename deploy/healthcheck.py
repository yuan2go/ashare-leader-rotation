"""Probe the container API on loopback without using a system HTTP proxy."""
import base64
import os
from http.client import HTTPConnection


def main():
    headers = {}
    user = os.environ.get("PHASE7A_BASIC_AUTH_USER")
    password = os.environ.get("PHASE7A_BASIC_AUTH_PASS")
    if user and password:
        credentials = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        headers["Authorization"] = f"Basic {credentials}"
    connection = HTTPConnection("127.0.0.1", 8000, timeout=5)
    try:
        connection.request("GET", "/api/v1/health", headers=headers)
        with connection.getresponse() as response:
            return int(response.status != 200)
    except OSError:
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
