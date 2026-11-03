"""SSH/SFTP transport for the authorized WIW Hub deployment. Never log secrets."""

import argparse
import base64
import hashlib
import importlib.util
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]


class PendingSocket:
    def __init__(self, connection, pending):
        self.connection, self.pending = connection, pending

    def recv(self, size):
        if self.pending:
            result, self.pending = self.pending[:size], self.pending[size:]
            return result
        return self.connection.recv(size)

    def __getattr__(self, key):
        return getattr(self.connection, key)


def connect():
    directory = ROOT / "outputs/hub-deploy"
    directory.mkdir(parents=True, exist_ok=True)
    known = directory / "known_hosts"
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    if known.exists():
        client.load_host_keys(str(known))
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    spec = importlib.util.spec_from_file_location(
        "hub_proxy", ROOT / "funpayparsers/deploy/ssh_proxy.py"
    )
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)
    connection, pending = proxy.connect("205.172.56.135", 2223)
    try:
        client.connect(
            "205.172.56.135",
            port=2223,
            username="webbox",
            key_filename=str(ROOT / "ssh/webbox_ed25519"),
            sock=PendingSocket(connection, pending),
            timeout=20,
            banner_timeout=25,
            auth_timeout=25,
            allow_agent=False,
            look_for_keys=False,
        )
        client.save_host_keys(str(known))
    except BaseException:
        connection.close()
        raise
    return client


def run_script(client, path):
    stdin, stdout, stderr = client.exec_command("python3 -", timeout=60)
    stdin.write(path.read_text("utf-8"))
    stdin.channel.shutdown_write()
    # Scripts are deliberately responsible for safe, bounded output.
    output = stdout.read().decode("utf-8", errors="replace")
    errors = stderr.read().decode("utf-8", errors="replace")
    status = stdout.channel.recv_exit_status()
    print(output, end="")
    if errors:
        print(errors, end="")
    return status


def upload(client, local, remote):
    with client.open_sftp() as sftp:
        created = set()

        def mkdir(path):
            if path in created or path == "/":
                return
            mkdir(path.rsplit("/", 1)[0] or "/")
            try:
                sftp.stat(path)
            except FileNotFoundError:
                sftp.mkdir(path)
            created.add(path)

        for source in sorted(local.rglob("*")):
            if not source.is_file():
                continue
            relative = source.relative_to(local).as_posix()
            target = remote.rstrip("/") + "/" + relative
            mkdir(target.rsplit("/", 1)[0])
            sftp.put(str(source), target)
        print("Uploaded release files")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", type=Path)
    parser.add_argument("--upload", type=Path)
    parser.add_argument("--remote")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--fetch")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with connect() as client:
        key = client.get_transport().get_remote_server_key()
        print(
            "SSH fingerprint SHA256:"
            + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
        )
        if args.snapshot:
            names = [
                "marketwatch/app.py",
                "marketwatch/config.py",
                "marketwatch/modules.py",
                "pyproject.toml",
                "requirements-server.txt",
                "deploy/nginx-ip.conf",
                "deploy/nginx-webbox.conf",
                "compose.yaml",
                "deploy/compose.webbox.yaml",
                "Dockerfile",
            ]
            with client.open_sftp() as sftp:
                for name in names:
                    target = args.snapshot / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    sftp.get("/opt/goldscope/" + name, str(target))
            print("Read-only hub source snapshot downloaded")
        if args.fetch:
            if (
                not args.fetch.startswith("/home/webbox/")
                or not args.output
                or not args.output.resolve().is_relative_to(ROOT / "outputs")
            ):
                raise ValueError("Private fetch must use deployment staging and local outputs")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with client.open_sftp() as sftp:
                sftp.get(args.fetch, str(args.output))
            args.output.chmod(0o600)
            print("Private verification file transferred without output")
        if args.upload:
            if not args.remote or not args.remote.startswith("/home/webbox/"):
                raise ValueError("Upload staging must be inside /home/webbox/")
            upload(client, args.upload, args.remote)
        if args.script:
            raise SystemExit(run_script(client, args.script))
