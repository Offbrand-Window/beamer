import socket
import socketserver
import time
from pathlib import Path


def _handle_instance_request(request, _client_address, server):
    with request.makefile("rb") as stream:
        if stream.readline(32).strip() == b"activate":
            server.activate()


class InstanceSignalServer(socketserver.ThreadingUnixStreamServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, path: Path, activate):
        self.path = Path(path)
        self.activate = activate
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        super().__init__(str(self.path), _handle_instance_request)
        self.path.chmod(0o600)

    def close(self):
        self.shutdown()
        self.server_close()
        self.path.unlink(missing_ok=True)


def request_running_instance(path: Path, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(str(path))
            client.sendall(b"activate\n")
            return True
        except (FileNotFoundError, ConnectionRefusedError):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        finally:
            client.close()


def instance_socket_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "Beamer Bluetooth" / "Beamer.sock"
