"""Bounded Unix-socket worker transport for the arm-private board.

The controller creates Board and BoardServer outside worker namespaces, then P7
mounts only this arm's socket plus the worker's own session token. Controller
operations are deliberately absent. Tokens must never enter logs or public argv.
"""
import base64
import binascii
import json
from pathlib import Path
import socket
import socketserver

from .board import BoardClient, BoardError, canonical

MAX_FRAME_BYTES = 3 * 1024 * 1024


def _wire(value):
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {key: _wire(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wire(val) for val in value]
    return value


def _unwire(value):
    if isinstance(value, dict):
        if set(value) == {"$bytes"}:
            if not isinstance(value["$bytes"], str):
                raise BoardError("invalid binary field")
            try:
                return base64.b64decode(value["$bytes"], validate=True)
            except (ValueError, binascii.Error) as exc:
                raise BoardError("invalid binary field") from exc
        return {key: _unwire(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_unwire(val) for val in value]
    return value


class _Handler(socketserver.StreamRequestHandler):
    timeout = 10

    def handle(self):
        try:
            line = self.rfile.readline(MAX_FRAME_BYTES + 1)
            if len(line) > MAX_FRAME_BYTES or not line.endswith(b"\n"):
                raise BoardError("oversized or incomplete request")
            request = json.loads(line)
            if not isinstance(request, dict) or set(request) != {"token", "arm_id", "problem_id", "operation", "arguments"}:
                raise BoardError("invalid request shape")
            if not isinstance(request["arguments"], dict):
                raise BoardError("invalid arguments")
            operation = request["operation"]
            if not isinstance(operation, str):
                raise BoardError("operation must be a name, e.g. post")
            operation = BoardClient.OP_ALIASES.get(operation.strip().lower(), operation.strip().lower())
            client = BoardClient(self.server.board, request["token"],
                                 request["arm_id"], request["problem_id"])
            result = client.call(operation, **_unwire(request["arguments"]))
            if (self.server.after_mutation is not None and operation in
                    {"post", "post_initial", "post_note", "post_help", "heartbeat", "post_result", "commit_review", "withdraw", "post_claim", "post_finding", "release_claim", "post_comment", "post_plan", "planner_page"}):
                self.server.after_mutation(token=request["token"], operation=operation)
            response = {"ok": True, "result": _wire(result)}
            encoded = canonical(response).encode() + b"\n"
            if len(encoded) > MAX_FRAME_BYTES:
                raise BoardError("response oversized; no partial evidence returned")
        except (BoardError, ValueError, TypeError, KeyError, RecursionError) as exc:
            # Never return private SQL rows, tokens or stack traces; return the
            # validation message so the model can correct its call (diagnosis
            # 2026-10-06: an opaque denial cost $22.30 of unposted work).
            message = str(exc)[:600] if isinstance(exc, (BoardError, ValueError, TypeError, KeyError)) else 'invalid request'
            encoded = canonical({"ok": False, "error": "board request denied: " + message}).encode() + b"\n" 
        except Exception:
            encoded = b'{"ok":false,"error":"board-unavailable"}\n'
        try:
            self.wfile.write(encoded)
        except (OSError, TimeoutError):
            pass  # A lost response does not roll back an already committed record.


class BoardServer(socketserver.UnixStreamServer):
    """Single bounded service thread; the board itself serializes all transactions.

    P7 owns this server's process lifetime and stop/durability watchdog. An existing
    socket is never silently unlinked, avoiding replacement of a live service.
    """
    allow_reuse_address = False
    request_queue_size = 16

    def __init__(self, socket_path, board, *, after_mutation=None):
        self.board = board
        self.after_mutation = after_mutation
        self.socket_path = Path(socket_path).absolute()
        self._socket_inode = None
        if any(path.is_symlink() for path in (self.socket_path, *self.socket_path.parents)):
            raise BoardError("symlink socket path")
        super().__init__(str(self.socket_path), _Handler)
        self.socket_path.chmod(0o600)

    def server_bind(self):
        super().server_bind()
        self._socket_inode = self.socket_path.stat().st_ino

    def server_close(self):
        super().server_close()
        # Parent must be service-owned; never put this socket in worker-writable storage.
        if self._socket_inode is not None:
            try:
                if self.socket_path.lstat().st_ino == self._socket_inode:
                    self.socket_path.unlink()
            except FileNotFoundError:
                pass

    def handle_error(self, request, client_address):
        # socketserver's default prints tracebacks; don't expose request context.
        pass


class RemoteBoardClient:
    def __init__(self, socket_path, *, token, arm_id, problem_id, timeout=15):
        self.socket_path, self.token = str(socket_path), token
        self.arm_id, self.problem_id, self.timeout = arm_id, problem_id, timeout

    def call(self, operation, **arguments):
        if operation not in BoardClient.OPERATIONS:
            raise BoardError("unknown worker operation")
        request = {"token": self.token, "arm_id": self.arm_id, "problem_id": self.problem_id,
                   "operation": operation, "arguments": _wire(arguments)}
        encoded = canonical(request).encode() + b"\n"
        if len(encoded) > MAX_FRAME_BYTES:
            raise BoardError("request oversized")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout)
            connection.connect(self.socket_path)
            connection.sendall(encoded)
            with connection.makefile("rb") as stream:
                line = stream.readline(MAX_FRAME_BYTES + 1)
        if len(line) > MAX_FRAME_BYTES or not line.endswith(b"\n"):
            raise BoardError("invalid board response")
        response = json.loads(line)
        if not response.get("ok"):
            raise BoardError(response.get("error", "board-unavailable"))
        return _unwire(response["result"])
