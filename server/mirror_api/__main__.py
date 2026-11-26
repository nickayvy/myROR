"""`python -m mirror_api [--host 0.0.0.0] [--port 8000]`"""

import argparse

import uvicorn


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    a = p.parse_args()
    uvicorn.run("mirror_api.app:create_app", factory=True, host=a.host, port=a.port,
                proxy_headers=False)


if __name__ == "__main__":
    main()
