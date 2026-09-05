"""Launch the local operator console: python -m scripts.serve."""
import argparse
import uvicorn


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    uvicorn.run("src.web.app:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
