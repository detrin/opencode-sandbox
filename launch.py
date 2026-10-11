import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import namedtuple
from functools import reduce
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOME = Path.home()
PROFILE_DIRS = [ROOT / "local", ROOT / "profiles"]
Agent = namedtuple("Agent", "cmd env readable protected")
SYSTEM_PATH = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]


def expand(p):
    return Path(p).expanduser()


def xdg(state):
    return {f"XDG_{k.upper()}_HOME": str(state / k) for k in ("config", "data", "cache", "state")}


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def merge(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return {**a, **{k: merge(a[k], v) if k in a else v for k, v in b.items()}}
    if isinstance(a, list) and isinstance(b, list):
        return a + [x for x in b if x not in a]
    return b


def env_value(m):
    name, default = m.group(1), m.group(2)
    value = os.environ.get(name) or default
    if value is None:
        sys.exit(f"profile needs ${{{name}}}; set it in your shell or give a default with ${{{name}:-...}}")
    return value


def subst(v):
    if isinstance(v, str):
        return re.sub(r"\$\{(\w+)(?::-([^}]*))?\}", env_value, v)
    if isinstance(v, dict):
        return {subst(k): subst(x) for k, x in v.items()}
    if isinstance(v, list):
        return [subst(x) for x in v]
    return v


def profile_names():
    return sorted({p.stem for d in PROFILE_DIRS if d.is_dir() for p in d.glob("*.json")} - {"policy"})


def load_json(name, dirs):
    return [json.loads(p.read_text()) for p in (d / f"{name}.json" for d in dirs) if p.is_file()]


def load_profile(name, chain=()):
    if name in chain:
        sys.exit(f"profile cycle: {' -> '.join((*chain, name))}")
    layers = load_json(name, reversed(PROFILE_DIRS))
    if not layers:
        sys.exit(f"unknown profile {name!r}; available: {', '.join(profile_names())}")
    base = {}
    for layer in layers:
        for parent in layer.pop("extends", []):
            base = merge(base, load_profile(parent, (*chain, name)))
        base = merge(base, layer)
    return base


def secret(name):
    value = (
        os.environ.get(name)
        or subprocess.run(
            ["security", "find-generic-password", "-a", os.environ["USER"], "-s", name, "-w"], capture_output=True, text=True
        ).stdout.strip()
    )
    if not value:
        print(f"warning: secret {name} not found in env or keychain", file=sys.stderr)
    return value


def aws_env(aws, d):
    d.mkdir(parents=True, exist_ok=True)
    creds = d / "creds.json"
    (d / "config").write_text(f"[profile sandbox]\nregion = {aws['region']}\ncredential_process = /bin/cat {creds}\n")

    def refresh():
        r = subprocess.run(
            ["aws", "configure", "export-credentials", "--profile", aws["profile"], "--format", "process"], capture_output=True, text=True
        )
        if r.returncode:
            return False
        c = json.loads(r.stdout)
        c["Expiration"] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        tmp = d / "creds.tmp"
        tmp.write_text(json.dumps(c))
        tmp.replace(creds)
        return True

    if not refresh():
        sys.exit(f"no AWS credentials for profile {aws['profile']}")

    def loop():
        while True:
            time.sleep(60)
            refresh()

    threading.Thread(target=loop, daemon=True).start()
    return {
        "AWS_CONFIG_FILE": str(d / "config"),
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "AWS_PROFILE": "sandbox",
        "AWS_REGION": aws["region"],
        "AWS_DEFAULT_REGION": aws["region"],
    }


def opencode(cfg, state, args):
    oc = merge(json.loads((ROOT / "opencode.json").read_text()), cfg.get("opencode", {}))
    oc.setdefault("model", cfg["model"])
    oc.setdefault("small_model", oc["model"])
    write_json(state / "opencode.json", oc)
    bin_dir = state / "cache/opencode/bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    rg = shutil.which("rg") or str(HOME / ".cache/opencode/bin/rg")
    if not (bin_dir / "rg").exists() and Path(rg).exists():
        shutil.copy(rg, bin_dir / "rg")
    env = {
        **xdg(state),
        "OPENCODE_CONFIG": str(state / "opencode.json"),
        **{f"OPENCODE_DISABLE_{k}": "1" for k in ("AUTOUPDATE", "MODELS_FETCH", "LSP_DOWNLOAD", "SHARE", "DEFAULT_PLUGINS")},
    }
    return Agent([str(HOME / ".opencode/bin/opencode"), *args], env, [HOME / ".opencode"], [state / "opencode.json"])


def claude(cfg, state, args):
    binary = Path(shutil.which("claude") or sys.exit("claude not found on PATH")).resolve()
    cdir = state / "claude"
    cdir.mkdir(parents=True, exist_ok=True)
    if not (cdir / "skills").exists():
        (cdir / "skills").symlink_to(HOME / ".claude/skills")
    if not (cdir / ".claude.json").exists():
        (cdir / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True}))
    settings = merge(
        {"permissions": {"defaultMode": "bypassPermissions"}, "skipDangerousModePermissionPrompt": True}, cfg.get("claude", {})
    )
    write_json(cdir / "settings.json", settings)
    env = {
        **xdg(state),
        "CLAUDE_CONFIG_DIR": str(cdir),
        "ANTHROPIC_MODEL": cfg["model"],
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    return Agent([str(binary), *args], env, [binary.parent], [cdir / "settings.json"])


AGENTS = {"opencode": opencode, "claude": claude}


def srt_settings(cfg, workdir, state, agent):
    def skill_targets():
        for d in map(expand, cfg["readableHomePaths"]):
            if d.is_dir() and "skills" in d.name:
                yield from (e.resolve() for e in d.iterdir() if e.is_symlink())

    allow_read = {*map(expand, cfg["readableHomePaths"]), *skill_targets(), *agent.readable, ROOT, workdir}
    deny_write = [
        state / "aws",
        state / "srt-settings.json",
        ROOT / "policy.json",
        ROOT / "opencode.json",
        ROOT / "profiles",
        ROOT / "local",
        ROOT / "sandbox.sh",
        ROOT / "launch.py",
        ROOT / "pyproject.toml",
        *agent.protected,
    ]
    return {
        "allowPty": True,
        "network": {"allowedDomains": cfg["allowedDomains"], "deniedDomains": []},
        "filesystem": {
            "denyRead": [str(HOME), *(str(expand(p)) for p in cfg["deniedHomePaths"]), *(str(HOME / p) for p in cfg["deniedPatterns"])],
            "allowRead": sorted(map(str, allow_read)),
            "allowWrite": [str(workdir), str(state), "/tmp", "/private/tmp", "/private/var/folders"],
            "denyWrite": sorted(map(str, deny_write)),
        },
    }


def parse_args(argv, default):
    name = os.environ.get("OCSB_PROFILE") or default
    if argv[:1] == ["--profile"]:
        if len(argv) < 2:
            sys.exit("--profile needs a name")
        name, argv = argv[1], argv[2:]
    exec_cmd = argv[1:] if argv[:1] == ["--exec"] else None
    if exec_cmd == []:
        sys.exit("--exec needs a command")
    return name, argv, exec_cmd


def resolve_workdir():
    workdir = expand(os.environ.get("OCSB_WORKDIR") or ROOT / "workspace").resolve()
    if workdir in (HOME, Path("/"), HOME / "projects"):
        sys.exit(f"refusing to grant write access to {workdir}")
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


def build_env(cfg, state, agent):
    path = [str(Path(agent.cmd[0]).parent), *(str(expand(p)) for p in cfg.get("path", [])), *SYSTEM_PATH]
    return {
        "HOME": str(HOME),
        "USER": os.environ["USER"],
        "LOGNAME": os.environ["USER"],
        "SHELL": "/bin/bash",
        "TERM": os.environ.get("TERM", "xterm-256color"),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "COLORTERM": os.environ.get("COLORTERM", ""),
        "TMPDIR": "/tmp",
        "PATH": ":".join(path),
        **agent.env,
        **(aws_env(cfg["aws"], state / "aws") if "aws" in cfg else {}),
        **{k: secret(k) for k in cfg.get("secrets", [])},
        **cfg.get("env", {}),
    }


def run(cmd, cwd, env):
    child = subprocess.Popen(cmd, cwd=cwd, env=env)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda s, _: child.send_signal(s))
    return child.wait()


def main():
    base = reduce(merge, load_json("policy", [ROOT, ROOT / "local"]))
    name, args, exec_cmd = parse_args(sys.argv[1:], base.pop("defaultProfile"))
    if args[:1] == ["--list"]:
        print("\n".join(profile_names()))
        return 0
    cfg = subst(merge(base, load_profile(name)))
    if cfg.get("agent") not in AGENTS:
        sys.exit(f"profile {name!r} has agent {cfg.get('agent')!r}; expected one of {', '.join(AGENTS)}")

    workdir = resolve_workdir()
    state = ROOT / ".state" / name
    state.mkdir(parents=True, exist_ok=True)
    agent = AGENTS[cfg["agent"]](cfg, state, [] if exec_cmd else args)
    settings = state / "srt-settings.json"
    write_json(settings, srt_settings(cfg, workdir, state, agent))
    srt = [str(ROOT / "node_modules/.bin/srt"), "--settings", str(settings), "--", "/usr/bin/env", "-u", "NO_PROXY", "-u", "no_proxy"]
    return run([*srt, *(exec_cmd or agent.cmd)], workdir, build_env(cfg, state, agent))


if __name__ == "__main__":
    sys.exit(main())
