"""Deploy, run, and validate a project's code on a remote Linux host (e.g. a
Raspberry Pi) over SSH. Shells out to the system `ssh`/`scp`/`tar` - no
agent to install on the remote side, no extra Python dependency (no
paramiko), works from Windows/macOS/Linux as long as OpenSSH is on PATH.

Two workflows:

- Fast iteration (no commit needed): `sync` mirrors the local project's
  working tree (git-tracked + untracked, .gitignore-respecting) into a
  scratch directory on the remote host; `run` executes a command there
  (syncing first by default), and can `--fetch` files back afterwards (a
  rendered image, a log, test output) for local inspection.
- Real deploy: `deploy` runs `git pull` in the remote host's actual git
  checkout, optionally runs an install/setup command, and optionally
  restarts a systemd service.

This tool is project-agnostic: run it from inside whichever project you want
to deploy (or point it there with -C/--project-dir), with that project's own
rpi_remote.config.json describing the remote host.

Setup (once per target project):
    Copy rpi_remote.config.example.json to rpi_remote.config.json in the
    target project's repo root and fill in the remote host. Auth is
    whatever your system `ssh` already uses (key/agent/known_hosts) - this
    tool never touches credentials itself.

Usage (run from the target project's repo, or pass -C/--project-dir):
    python rpi_remote.py sync
    python rpi_remote.py run [--skip-sync] [--fetch REMOTE=LOCAL ...] -- <command>
    python rpi_remote.py fetch <remote_path> <local_path>
    python rpi_remote.py logs [-n 200] [-f]
    python rpi_remote.py status
    python rpi_remote.py deploy [--install-cmd "..."] [--restart]
    python rpi_remote.py shell
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

CONFIG_FILENAME = "rpi_remote.config.json"
EXAMPLE_CONFIG_FILENAME = "rpi_remote.config.example.json"


class ConfigError(Exception):
    pass


class RemoteCommandError(Exception):
    pass


def load_config(project_dir: Path):
    config_path = project_dir / CONFIG_FILENAME
    if not config_path.exists():
        raise ConfigError(
            f"Missing {config_path}.\n"
            f"Copy {EXAMPLE_CONFIG_FILENAME} to {CONFIG_FILENAME} in your project's repo root "
            "and fill in the remote host."
        )
    import json

    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("port", 22)
    cfg.setdefault("remote_dir", "~/rpi-remote-dev")
    cfg.setdefault("python", "python3")
    cfg.setdefault("service", None)
    cfg.setdefault("repo_dir", None)
    for key in ("host", "user"):
        if not cfg.get(key):
            raise ConfigError(f"'{key}' missing from {config_path}")
    return cfg


def ssh_target(cfg):
    return f"{cfg['user']}@{cfg['host']}"


def run_ssh(cfg, remote_command, tty=False, check=True):
    cmd = ["ssh"]
    if tty:
        cmd.append("-t")
    cmd += ["-p", str(cfg["port"]), ssh_target(cfg), remote_command]
    print(f"$ ssh {ssh_target(cfg)} '{remote_command}'")
    result = subprocess.run(cmd)
    if check and result.returncode != 0:
        raise RemoteCommandError(f"remote command failed (exit {result.returncode}): {remote_command}")
    return result


def scp_from_remote(cfg, remote_path, local_path):
    cmd = ["scp", "-P", str(cfg["port"]), f"{ssh_target(cfg)}:{remote_path}", str(local_path)]
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def cmd_sync(cfg, project_dir, args):
    """Mirror the local project's working tree to remote_dir via `git ls-files` + tar-over-ssh."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "-c", "-o", "--exclude-standard"],
        cwd=project_dir, check=True, capture_output=True,
    )
    files = [f for f in listed.stdout.decode("utf-8").split("\0") if f]
    if not files:
        print("Nothing to sync - `git ls-files` returned no tracked/untracked files.")
        return

    manifest = project_dir / ".rpi_remote_manifest.tmp"
    manifest.write_text("\n".join(files), encoding="utf-8")
    try:
        run_ssh(cfg, f"rm -rf {cfg['remote_dir']} && mkdir -p {cfg['remote_dir']}")

        tar_proc = subprocess.Popen(
            ["tar", "-czf", "-", "-T", str(manifest)],
            cwd=project_dir, stdout=subprocess.PIPE,
        )
        ssh_proc = subprocess.Popen(
            ["ssh", "-p", str(cfg["port"]), ssh_target(cfg), f"tar -xzf - -C {cfg['remote_dir']}"],
            stdin=tar_proc.stdout,
        )
        tar_proc.stdout.close()
        ssh_proc.communicate()
        tar_proc.wait()
        if tar_proc.returncode != 0 or ssh_proc.returncode != 0:
            raise RemoteCommandError("sync failed (tar/ssh pipe returned non-zero)")
    finally:
        manifest.unlink(missing_ok=True)

    print(f"Synced {len(files)} files to {ssh_target(cfg)}:{cfg['remote_dir']}")


def _parse_fetch_spec(spec):
    if "=" not in spec:
        raise SystemExit(f"error: --fetch expects REMOTE=LOCAL, got: {spec}")
    remote_path, local_path = spec.split("=", 1)
    return remote_path, local_path


def cmd_run(cfg, project_dir, args):
    """Sync, then run an arbitrary command on the remote host inside remote_dir, then optionally
    fetch files back."""
    if not args.skip_sync:
        cmd_sync(cfg, project_dir, args)

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print('error: no command given, e.g. `run -- python3 -c "print(1)"`', file=sys.stderr)
        sys.exit(1)

    python_dir = os.path.dirname(cfg["python"])
    path_prefix = f"PATH={python_dir}:$PATH " if python_dir else ""
    remote_cmd = f"cd {cfg['remote_dir']} && {path_prefix}" + " ".join(command)
    run_ssh(cfg, remote_cmd)

    for spec in args.fetch or []:
        remote_path, local_path = _parse_fetch_spec(spec)
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        scp_from_remote(cfg, remote_path, local_path)
        print(f"Fetched {local_path}")


def cmd_fetch(cfg, project_dir, args):
    local_path = Path(args.local_path)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    scp_from_remote(cfg, args.remote_path, local_path)
    print(f"Fetched {local_path}")


def cmd_logs(cfg, project_dir, args):
    if not cfg.get("service"):
        print("error: 'service' is not set in the config - nothing to look up logs for.", file=sys.stderr)
        sys.exit(1)
    remote_cmd = f"journalctl -u {cfg['service']} -n {args.lines}"
    if args.follow:
        remote_cmd += " -f"
    run_ssh(cfg, remote_cmd, tty=True, check=False)


def cmd_status(cfg, project_dir, args):
    if not cfg.get("service"):
        print("error: 'service' is not set in the config - nothing to check status for.", file=sys.stderr)
        sys.exit(1)
    run_ssh(cfg, f"systemctl status {cfg['service']} --no-pager", check=False)


def cmd_deploy(cfg, project_dir, args):
    """git pull the remote host's real checkout, optionally run an install command, optionally
    restart a systemd service."""
    if not cfg.get("repo_dir"):
        print("error: 'repo_dir' is not set in the config - nothing to deploy to.", file=sys.stderr)
        sys.exit(1)

    steps = [f"cd {cfg['repo_dir']}", "git pull"]
    if args.install_cmd:
        steps.append(args.install_cmd)
    run_ssh(cfg, " && ".join(steps))

    if args.restart:
        if not cfg.get("service"):
            print("error: --restart needs 'service' set in the config.", file=sys.stderr)
            sys.exit(1)
        run_ssh(cfg, f"sudo systemctl restart {cfg['service']}")
        print("Restarted service. Tailing logs (Ctrl-C to stop tailing)...")
        cmd_logs(cfg, project_dir, argparse.Namespace(lines=40, follow=True))


def cmd_shell(cfg, project_dir, args):
    subprocess.run(["ssh", "-p", str(cfg["port"]), ssh_target(cfg)])


def build_parser():
    parser = argparse.ArgumentParser(
        description="Deploy, run, and validate a project's code on a remote host over SSH.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-C", "--project-dir", default=".",
        help="Path to the project to deploy (default: current directory). Must contain "
             f"{CONFIG_FILENAME} and be a git repo.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sync_p = sub.add_parser("sync", help="Mirror the local project's working tree to remote_dir")
    sync_p.set_defaults(func=cmd_sync)

    run_p = sub.add_parser("run", help="Sync + run an arbitrary command on the remote host in remote_dir")
    run_p.add_argument("--skip-sync", action="store_true", help="Reuse whatever is already in remote_dir")
    run_p.add_argument(
        "--fetch", action="append", metavar="REMOTE=LOCAL",
        help="Copy a file back after the command runs; repeatable",
    )
    run_p.add_argument(
        "command", nargs=argparse.REMAINDER,
        help='Command to run, e.g. -- python3 main.py --mock-output out.png',
    )
    run_p.set_defaults(func=cmd_run)

    fetch_p = sub.add_parser("fetch", help="Copy a file back from the remote host")
    fetch_p.add_argument("remote_path")
    fetch_p.add_argument("local_path")
    fetch_p.set_defaults(func=cmd_fetch)

    logs_p = sub.add_parser("logs", help="Tail the configured systemd service's journalctl logs")
    logs_p.add_argument("-n", "--lines", type=int, default=100)
    logs_p.add_argument("-f", "--follow", action="store_true")
    logs_p.set_defaults(func=cmd_logs)

    status_p = sub.add_parser("status", help="Show the configured systemd service's status")
    status_p.set_defaults(func=cmd_status)

    deploy_p = sub.add_parser(
        "deploy", help="git pull the real installed checkout on the remote host (production deploy flow)"
    )
    deploy_p.add_argument(
        "--install-cmd", help="Extra shell command to run after git pull (e.g. a dependency install)",
    )
    deploy_p.add_argument(
        "--restart", action="store_true",
        help="Restart the configured systemd service afterwards and tail its logs to confirm",
    )
    deploy_p.set_defaults(func=cmd_deploy)

    shell_p = sub.add_parser("shell", help="Open an interactive SSH session to the remote host")
    shell_p.set_defaults(func=cmd_shell)

    return parser


def main():
    args = build_parser().parse_args()
    project_dir = Path(args.project_dir).resolve()
    try:
        cfg = load_config(project_dir)
        args.func(cfg, project_dir, args)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except RemoteCommandError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"error: command failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
