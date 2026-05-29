"""
build_protos.py
Compiles proto/MarketDataFeed.proto → proto/MarketDataFeed_pb2.py
using grpcio-tools (which bundles its own protoc binary).

Run once before starting the engine:
    python build_protos.py

Or from the deploy script:
    .venv/bin/python build_protos.py
"""

import os
import sys


def main() -> int:
    proto_dir  = os.path.join(os.path.dirname(__file__), "proto")
    proto_file = os.path.join(proto_dir, "MarketDataFeed.proto")

    if not os.path.exists(proto_file):
        print(f"ERROR: proto file not found at {proto_file}", file=sys.stderr)
        return 1

    try:
        from grpc_tools import protoc
    except ImportError:
        print(
            "ERROR: grpcio-tools not installed.\n"
            "Run:  pip install grpcio-tools",
            file=sys.stderr,
        )
        return 1

    # protoc args:
    #   -I proto_dir          → search path for imports
    #   --python_out=proto_dir → write _pb2.py into proto/
    #   proto_file            → file to compile
    result = protoc.main([
        "grpc_tools.protoc",
        f"-I{proto_dir}",
        f"--python_out={proto_dir}",
        proto_file,
    ])

    if result == 0:
        pb2_path = os.path.join(proto_dir, "MarketDataFeed_pb2.py")
        if os.path.exists(pb2_path):
            print(f"✓  Compiled → {pb2_path}")
        else:
            print("WARNING: protoc exited 0 but _pb2.py not found", file=sys.stderr)
    else:
        print(f"ERROR: protoc exited with code {result}", file=sys.stderr)

    return result


if __name__ == "__main__":
    sys.exit(main())
