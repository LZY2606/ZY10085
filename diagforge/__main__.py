"""CLI entry: python3 -m diagforge --host 127.0.0.1 --port 5214"""
import argparse

from .server import serve


def main():
    parser = argparse.ArgumentParser(prog="diagforge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5214)
    parser.add_argument("--db", default="diagforge.db")
    args = parser.parse_args()
    serve(args.host, args.port, args.db)


if __name__ == "__main__":
    main()
