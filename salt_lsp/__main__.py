import argparse
import logging
import signal
import sys
from typing import Dict

from salt_lsp.server import SaltServer, setup_salt_server_capabilities


LOG_LEVEL_DICT: Dict[str, int] = {
    "critical": logging.CRITICAL,
    "fatal": logging.FATAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "warn": logging.WARN,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}


def loglevel_from_str(level: str) -> int:
    if level.lower() not in LOG_LEVEL_DICT:
        return logging.DEBUG
    return LOG_LEVEL_DICT[level.lower()]


def add_arguments(parser):
    parser.description = "salt state server"

    parser.add_argument(
        "--tcp", action="store_true", help="Use TCP server instead of stdio"
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="Bind to this address"
    )
    parser.add_argument(
        "--port", type=int, default=2087, help="Bind to this port"
    )
    parser.add_argument(
        "--stop-after-init",
        action="store_true",
        help="initialize the server, but don't launch it "
        "(useful for debugging/testing purposes)",
    )
    parser.add_argument(
        "--log-level",
        choices=list(LOG_LEVEL_DICT.keys())
        + list(map(lambda level: level.upper(), LOG_LEVEL_DICT.keys())),
        default=["debug"],
        nargs=1,
        help="Logging verbosity",
    )


def main():
    parser = argparse.ArgumentParser()
    add_arguments(parser)
    args = parser.parse_args()

    log_level = loglevel_from_str(args.log_level[0])
    logging.basicConfig(
        level=log_level,
    )

    salt_server = SaltServer()
    setup_salt_server_capabilities(salt_server)
    # Until versioned API catalogs are supplied, metadata is deliberately empty.
    # Never load developer-generated pickle files or invoke Salt on startup.
    salt_server.post_init({}, log_level)

    if args.stop_after_init:
        return

    # Editors stop servers with SIGTERM. Unwinding through the event loop lets
    # cancellation clean up any running external-tool process group.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    if args.tcp:
        salt_server.start_tcp(args.host, args.port)
    else:
        salt_server.exit_process_on_shutdown = True
        salt_server.start_io()


if __name__ == "__main__":
    main()
