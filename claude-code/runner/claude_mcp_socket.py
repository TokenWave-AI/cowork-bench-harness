"""Unprivileged stdio transport to one controller-owned, run-bound MCP socket."""
import socket
import sys
import threading


def main():
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.connect(sys.argv[1])
    def input_stream():
        try:
            while data := sys.stdin.buffer.read1(65536):
                connection.sendall(data)
        finally:
            try: connection.shutdown(socket.SHUT_WR)
            except OSError: pass
    threading.Thread(target=input_stream, daemon=True).start()
    try:
        while data := connection.recv(65536):
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
    finally:
        connection.close()


if __name__ == '__main__':
    main()
