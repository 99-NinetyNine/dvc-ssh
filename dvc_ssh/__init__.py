import getpass
import os.path
import threading
from contextlib import suppress
from pathlib import Path, PurePath
from typing import ClassVar, Union

from asyncssh.config import SSHClientConfig
from funcy import memoize, silent, wrap_prop, wrap_with

from dvc.utils.objects import cached_property
from dvc_objects.fs.base import FileSystem
from dvc_objects.fs.utils import as_atomic

SSH_CONFIG = Path("~", ".ssh", "config").expanduser()
FilePath = Union[str, PurePath]

DEFAULT_PORT = 22


def parse_config(*, host, user=(), port=(), local_user=None, config_files=None):
    if config_files is None:
        config_files = [SSH_CONFIG]

    if local_user is None:
        with suppress(KeyError):
            local_user = getpass.getuser()

    last_config = None
    reload = False
    config = SSHClientConfig(
        last_config=last_config,
        reload=reload,
        canonical=False,
        final=False,
        local_user=local_user,
        user=user,
        host=host,
        port=port,
    )

    if config_files:
        if isinstance(config_files, (str, PurePath)):
            # paths: Sequence[FilePath] = [config_files] #lint issue
            paths = [config_files]
        else:
            paths = config_files

        for path in paths:
            config.parse(Path(path))
        config.loaded = True
    return config


@wrap_with(threading.Lock())
@memoize
def ask_password(host, user, port, desc):
    try:
        return getpass.getpass(
            f"Enter a {desc} for " f"host '{host}' port '{port}' user '{user}':\n"
        )
    except EOFError:
        return None


class SSHFileSystem(FileSystem):
    protocol = "ssh"
    REQUIRES: ClassVar[dict[str, str]] = {"sshfs": "sshfs"}
    PARAM_CHECKSUM = "md5"

    @classmethod
    def _strip_protocol(cls, path: str) -> str:
        from fsspec.utils import infer_storage_options

        return infer_storage_options(path)["path"]

    def unstrip_protocol(self, path: str) -> str:
        host = self.fs_args["host"]
        port = self.fs_args["port"]
        path = path.lstrip("/")
        return f"ssh://{host}:{port}/{path}"

    def _prepare_credentials(self, **config):
        # from sshfs.config import parse_config

        from .client import InteractiveSSHClient

        self.CAN_TRAVERSE = True

        login_info = {}
        login_info["client_factory"] = config.get(
            "client_factory", InteractiveSSHClient
        )
        try:
            user_ssh_config = parse_config(host=config["host"])
        except FileNotFoundError:
            user_ssh_config = {}

        login_info["host"] = user_ssh_config.get("Hostname", config["host"])

        login_info["username"] = (
            config.get("user")
            or config.get("username")
            or user_ssh_config.get("User")
            or getpass.getuser()
        )
        login_info["port"] = (
            config.get("port")
            or silent(int)(user_ssh_config.get("Port"))
            or DEFAULT_PORT
        )

        for option in ("password", "passphrase"):
            login_info[option] = config.get(option)

            if config.get(f"ask_{option}") and login_info[option] is None:
                login_info[option] = ask_password(
                    login_info["host"],
                    login_info["username"],
                    login_info["port"],
                    option,
                )

        raw_keys = []
        if config.get("keyfile"):
            raw_keys.append(config.get("keyfile"))
        elif user_ssh_config.get("IdentityFile"):
            raw_keys.extend(user_ssh_config.get("IdentityFile"))

        if raw_keys:
            login_info["client_keys"] = [os.path.expanduser(key) for key in raw_keys]

        login_info["timeout"] = config.get("timeout", 1800)

        # These two settings fine tune the asyncssh to use the
        # fastest encryption algorithm and disable compression
        # altogether (since it blocking, it is slowing down
        # the transfers in a considerable rate, and even for
        # compressible data it is making it extremely slow).
        # See: https://github.com/ronf/asyncssh/issues/374
        login_info["encryption_algs"] = [
            "aes128-gcm@openssh.com",
            "aes256-ctr",
            "aes192-ctr",
            "aes128-ctr",
        ]
        login_info["compression_algs"] = None

        login_info["gss_auth"] = config.get("gss_auth", False)
        login_info["agent_forwarding"] = config.get("agent_forwarding", True)
        login_info["proxy_command"] = user_ssh_config.get("ProxyCommand")

        # We are going to automatically add stuff to known_hosts
        # something like paramiko's AutoAddPolicy()
        login_info["known_hosts"] = None

        if "max_sessions" in config:
            login_info["max_sessions"] = config["max_sessions"]
        return login_info

    @wrap_prop(threading.Lock())
    @cached_property
    def fs(self):
        from sshfs import SSHFileSystem as _SSHFileSystem

        return _SSHFileSystem(**self.fs_args)

    # Ensure that if an interrupt happens during the transfer, we don't
    # pollute the cache.

    def upload_fobj(self, fobj, to_info, **kwargs):
        with as_atomic(self, to_info) as tmp_file:
            super().upload_fobj(fobj, tmp_file, **kwargs)

    def put_file(
        self,
        from_file,
        to_info,
        **kwargs,
    ):
        with as_atomic(self, to_info) as tmp_file:
            super().put_file(from_file, tmp_file, **kwargs)
