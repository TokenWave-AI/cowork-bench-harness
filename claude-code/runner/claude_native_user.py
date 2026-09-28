"""Run the native CLI unprivileged; relay only its run-bound MCP to the controller."""
import os
from pathlib import Path
import pwd
import socket
import subprocess
import threading
import uuid


def identity(config):
    account = pwd.getpwnam(config['native_solver_user'])
    if account.pw_uid == 0 or account.pw_gid == 0:
        raise RuntimeError('Native solver identity must be non-root')
    return account


def native_environment(config, workspace):
    account = identity(config)
    workspace = Path(workspace)
    # The native client can traverse to its isolated working directory, but
    # cannot enumerate root-owned jobs or read private task/controller files.
    campaign = workspace.parent.parent
    if workspace.parent.name != 'agent-workdirs' or not (campaign/'CONFIG.json').is_file():
        raise RuntimeError('Native workspace must belong to this prepared campaign')
    for parent in (campaign, workspace.parent):
        parent.chmod(parent.stat().st_mode | 0o111)
    workspace.chmod(0o700)
    os.chown(workspace, account.pw_uid, account.pw_gid)
    env = dict(os.environ)
    for name, folder in [('CLAUDE_CONFIG_DIR', '.claude'), ('TMPDIR', '.tmp'), ('XDG_CACHE_HOME', '.cache')]:
        path = workspace / folder
        path.mkdir(mode=0o700, exist_ok=True)
        os.chown(path, account.pw_uid, account.pw_gid)
        env[name] = str(path)
    return env


def drop_identity(config):
    account = identity(config)
    return dict(user=account.pw_uid, group=account.pw_gid, extra_groups=[])


class MCPRelay:
    """One Unix connection, one trusted MCP process, no general command broker."""
    def __init__(self, config, command, env):
        self.account = identity(config)
        self.command, self.env = command, env
        directory = Path(config['native_mcp_relay_root'])
        if not directory.is_dir():
            raise RuntimeError('Native MCP relay directory was not provisioned')
        self.path = directory / (uuid.uuid4().hex + '.sock')
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        self.path.chmod(0o600)
        os.chown(self.path, self.account.pw_uid, self.account.pw_gid)
        self.server.listen(1)
        self.server.settimeout(1)
        self.stopped = threading.Event()
        self.child = None
        self.connection = None
        self.errors = []

    def serve(self):
        try:
            while not self.stopped.is_set():
                try:
                    connection, _ = self.server.accept()
                    break
                except socket.timeout:
                    continue
            else:
                return
            self.connection = connection
            # SO_PEERCRED binds the bridge to the configured unprivileged UID.
            import struct
            _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != self.account.pw_uid:
                raise RuntimeError('Unexpected native MCP client identity')
            self.child = subprocess.Popen(self.command, env=self.env, stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                          start_new_session=True)
            child = self.child
            def forward_input():
                try:
                    while data := connection.recv(65536):
                        child.stdin.write(data)
                        child.stdin.flush()
                except (OSError, BrokenPipeError):
                    pass
                finally:
                    try: child.stdin.close()
                    except OSError: pass
            reader = threading.Thread(target=forward_input, daemon=True)
            reader.start()
            while data := child.stdout.read1(65536):
                connection.sendall(data)
        except OSError as error:
            if not self.stopped.is_set(): self.errors.append(type(error).__name__)
        except Exception as error:
            self.errors.append(type(error).__name__)
        finally:
            if self.connection:
                try: self.connection.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                self.connection.close()

    def __enter__(self):
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stopped.set()
        self.server.close()
        if self.connection:
            try: self.connection.shutdown(socket.SHUT_RDWR)
            except OSError: pass
        if self.child:
            # The owned process group includes the fixed runtime MCP descendant.
            import signal
            try: os.killpg(self.child.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            try: self.child.wait(timeout=6)
            except subprocess.TimeoutExpired:
                try: os.killpg(self.child.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                self.child.wait(timeout=5)
        self.thread.join(timeout=3)
        self.path.unlink(missing_ok=True)


def write_native_config(path, value, config):
    import json
    path.write_text(json.dumps(value) + '\n')
    account = identity(config)
    os.chown(path, account.pw_uid, account.pw_gid)
    path.chmod(0o600)
