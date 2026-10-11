# opencode-sandbox

A thin launcher around [`@anthropic-ai/sandbox-runtime`](https://www.npmjs.com/package/@anthropic-ai/sandbox-runtime) (srt) that runs a coding agent (opencode or Claude Code) on macOS (srt also supports Linux; this launcher is only tested on macOS) with:
- a network allowlist
- read-only access to your skills and `~/projects`
- an empty environment, apart from the credentials the chosen profile asks for

Which agent, model and provider is a **profile**.

```bash
./sandbox.sh                                        # default profile (claude-bedrock), TUI in ./workspace
./sandbox.sh --profile kimi-bedrock                 # opencode with Kimi K3 on Bedrock
./sandbox.sh --profile openrouter run "..."         # anything after the profile goes to the agent
OCSB_PROFILE=ollama-remote OLLAMA_SERVER=gpu-box:11434 ./sandbox.sh
OCSB_WORKDIR=~/some/project ./sandbox.sh            # work in another folder (it becomes writable)
./sandbox.sh --profile openrouter --exec bash       # a plain shell under the same sandbox
./sandbox.sh --list
```

## Profiles

| Profile | Agent | Model / provider | Credentials | Network |
|---|---|---|---|---|
| `claude-bedrock` | Claude Code | Opus 5.5 on Bedrock | AWS profile `OCSB_AWS_PROFILE` (default `default`), region `OCSB_AWS_REGION` (default `us-east-1`) | Bedrock runtime in that region |
| `kimi-bedrock` | opencode | Kimi K3 on Bedrock | same as above | Bedrock runtime in that region |
| `openrouter` | opencode | `OPENROUTER_MODEL`, default `moonshotai/kimi-k3` | `OPENROUTER_API_KEY` | openrouter.ai |
| `ollama-remote` | opencode | `OLLAMA_MODEL` on `OLLAMA_SERVER` (`host:port`) | none | that host and port only |

A profile is a JSON file in `profiles/`. The gitignored `local/` directory is layered on top: `local/<name>.json` is merged over `profiles/<name>.json` (or stands alone as a private profile), and `local/policy.json` is merged over `policy.json`. Keep machine-specific accounts, internal hosts and your preferred `defaultProfile` there, for example:

```jsonc
// local/claude-bedrock.json: same profile, different AWS account
{"aws": {"profile": "work-bedrock"}}

// local/kimi-work.json: a private profile that adds an internal service
{"extends": ["kimi-bedrock"], "allowedDomains": ["data.internal.example.com:443"], "secrets": ["DATA_API_TOKEN"]}

// local/policy.json
{"defaultProfile": "kimi-work"}
```

`OCSB_PROFILE` or `defaultProfile` picks the profile when `--profile` is not given. All profile keys:

```jsonc
{
  "extends": ["kimi-bedrock"],               // merged first; lists append, objects merge, scalars override
  "agent": "opencode",                       // opencode | claude
  "model": "amazon-bedrock/us.moonshotai.kimi-k3",
  "aws": {"profile": "default", "region": "us-east-1"},         // optional: short-lived keys, refreshed every minute
  "secrets": ["DATA_API_TOKEN"],                                // copied from your env, else the macOS keychain
  "env": {"CLAUDE_CODE_USE_BEDROCK": "1"},                      // literal env vars
  "allowedDomains": ["bedrock-runtime.us-east-1.amazonaws.com:443"],
  "path": ["~/tools/bin"],                                      // prepended to PATH
  "readableHomePaths": [],                   // added to the base list in policy.json
  "opencode": {"provider": {}},              // merged into opencode.json (opencode agent)
  "claude": {}                               // merged into Claude Code settings.json (claude agent)
}
```

Any string, including object keys, can use `${VAR}` or `${VAR:-default}` from your shell. A missing variable with no default stops the launch.

To add a new setup, for example an agent that may only talk to one website, add a profile with that host in `allowedDomains` and, for opencode, `"opencode": {"permission": {"webfetch": "allow"}}`.

## What the sandbox allows

| | Allowed | Everything else |
|---|---|---|
| Network | the profile's `allowedDomains` (`host:port`) | proxy 403 |
| Read | `~/projects` (read-only), `~/.gitconfig`, `~/.claude/skills`, `~/.agents/skills` and their symlink targets, `~/.opencode`, the agent binary, the workdir | rest of `$HOME` unreadable, incl. `~/.aws`, `~/.ssh`, `~/Documents`; `.env*`, `.envrc`, `*.pem`, `*.key`, `*.tfvars`, `*.tfstate` unreadable anywhere |
| Write | the workdir, `.state/<profile>/`, `/tmp` | profiles, `policy.json`, `opencode.json`, the launcher and the generated agent and srt config are read-only |
| Env | the profile's `aws`, `secrets` and `env` | the rest of your shell env (API tokens, cloud credentials) is dropped |

`policy.json` holds the filesystem rules shared by every profile. `opencode.json` is the opencode base config that each opencode profile is merged into. Everything is regenerated on every launch.

## Development

`launch.py` uses only the standard library and runs on the system `python3` (3.9+); the venv is just for linting.

```bash
uv sync && source .venv/bin/activate
ruff check launch.py && ruff format --check launch.py
```

## Gotchas

- `deniedPatterns` in `policy.json` are anchored at `$HOME`; srt resolves relative globs against the workdir, so a bare `**/.env` would only cover `workspace/`.
- `git status` in a repo prints `Operation not permitted` for each denied file (`.envrc`, `*.tfstate` fixtures) and otherwise works.
- AWS keys are re-exported from the host profile every minute, so refreshing your SSO session on the host is enough; the sandbox never sees `~/.aws`.
- `opencode run` sometimes exits before printing the final answer. The answer is stored, so read it with `./sandbox.sh export <session-id>`; the TUI is unaffected.
- `opencode export` with no session id waits for an interactive picker and holds the session DB, which hangs every other run. Always pass an id.
- `NpmInstallFailedError ... registry.npmjs.org` in the logs is opencode trying to install its plugin SDK; it is blocked on purpose and harmless.
- Services that hand out presigned download URLs (S3, GCS) redirect to hosts that are not on the allowlist; add them to the profile if the agent needs those downloads.
- `allowPty` is needed for the TUI; without it the sandbox denies terminal ioctls and opencode dies with `setRawMode failed with errno: 1`.
- srt exports `NO_PROXY` for loopback and RFC 1918 ranges, but the sandbox blocks every direct connection, so the launcher clears it. Every request goes through the proxy and the allowlist alone decides, which is what makes `192.168.x.y:11434` or `localhost:11434` reachable when listed.
- The proxy refuses a hostname that resolves to this machine's own addresses (`my-mac.local`). Use `localhost:PORT` for a server on the same machine and a hostname or IP for a remote one.
- Bedrock model IDs with a `us.` prefix are cross-region inference profiles; use the matching prefix (`eu.`, `apac.`) when you change `OCSB_AWS_REGION`.
- `.state/<profile>/` holds each profile's sessions and generated config. A Claude Code profile starts with its own empty config dir (`.state/<profile>/claude`) with your skills symlinked in, not your `~/.claude`.
