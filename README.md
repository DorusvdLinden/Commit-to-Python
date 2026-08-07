# rpi-remote

A small, dependency-free command-line tool to deploy, run, and validate a
project's code on a remote Linux host (a Raspberry Pi, or any SSH-reachable
box) - without committing every trial change first.

It shells out to the system `ssh`/`scp`/`tar`, so the only requirement on
either end is a normal OpenSSH install (present by default on Raspberry Pi
OS, Windows 10/11, and virtually every Linux distro). No agent to install on
the remote side, no paramiko or other Python dependency, no cloud service.

## Why

Iterating against real remote hardware usually means either:

- committing/pushing every trial change so `git pull` picks it up, or
- manually `scp`-ing files around and re-typing the same SSH commands.

`rpi-remote` mirrors your project's current working tree (including
uncommitted changes) to a scratch directory on the remote host on demand,
lets you run arbitrary commands there, and can pull result files (a
rendered image, a log, test output) straight back for inspection - then,
separately, wraps the "real" deploy flow (`git pull` + restart a service)
for once you're ready to commit.

## Setup

This tool is project-agnostic - it doesn't live inside the project you're
deploying. Run it from inside that project's directory (or point it there
with `-C`), and give that project its own config file:

```bash
cp /path/to/rpi_remote.config.example.json ./rpi_remote.config.json
```

Edit `rpi_remote.config.json` (gitignored - it's machine/host-specific, don't
commit it in your project):

```json
{
  "host": "raspberrypi.local",
  "user": "pi",
  "port": 22,
  "remote_dir": "~/rpi-remote-dev",
  "python": "python3",
  "service": null,
  "repo_dir": null
}
```

| Field | Meaning |
|---|---|
| `host` / `user` / `port` | SSH connection to the remote host |
| `remote_dir` | Scratch directory `sync`/`run` mirror the working tree into |
| `python` | Interpreter used for convenience `PATH` setup in `run` (e.g. a venv's `bin/python`) |
| `service` | Optional systemd unit name, for `logs`/`status`/`deploy --restart` |
| `repo_dir` | Optional path to the real git checkout on the remote host, for `deploy` |

Authentication is whatever your system `ssh` already does (key, agent,
`~/.ssh/config`, `known_hosts`) - this tool never touches credentials
itself.

## Usage

Run these from inside the target project (or add `-C /path/to/project`):

```bash
# Mirror the current working tree (including uncommitted changes) to the remote host
python rpi_remote.py sync

# Run a command remotely (syncs first by default)
python rpi_remote.py run -- python3 -c "import sys; print(sys.version)"

# Run a command and pull a result file back afterwards
python rpi_remote.py run --fetch out.png=./local_out.png -- python3 main.py --mock-output out.png

# Grab a file without running anything
python rpi_remote.py fetch /var/log/myapp.log ./myapp.log

# Check a configured systemd service
python rpi_remote.py status
python rpi_remote.py logs -f

# Deploy: git pull the real checkout, optionally reinstall deps, optionally restart + tail logs
python rpi_remote.py deploy --install-cmd "pip install -r requirements.txt" --restart

# Just open an interactive SSH session
python rpi_remote.py shell
```

## Notes

- `sync` mirrors exactly what `git ls-files -c -o --exclude-standard` reports
  for the target project - tracked files (with their current working-tree
  content, not the last commit) plus untracked-but-not-ignored files. It
  fully replaces the contents of `remote_dir` each time, so deleted local
  files don't linger remotely.
- `deploy` is intentionally separate from `sync`/`run`: it operates on the
  remote host's actual git checkout (`repo_dir`), not the scratch mirror, so
  a real/production deploy only ever runs from a committed, pushed state.
