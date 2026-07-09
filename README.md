# scorebench-cli

Public agent CLI for [ScoreBench](https://scorebench.dev/) (Harness) middleware
servers. Workers use it to read their scoped exercise, start and ping runs,
submit candidates, and view read-only venue data. It contains no server code,
no connector credentials, and no solutions.

## Install

```bash
pipx install git+https://github.com/josusanmartin/scorebench-cli
# or: pip install --user git+https://github.com/josusanmartin/scorebench-cli
```

Both `scorebench` and the legacy `harness` command are installed.

## Use

Workers receive only a scoped exercise API key:

```bash
export HARNESS_URL=https://scorebench.dev/
export HARNESS_RUN_TOKEN=hrun_...
harness context
harness exercise
harness run start --id run001 --skills scorebench --model <model> --effort <effort> --autonomy autonomous
harness run ping --event start
harness submit path/to/solution --total-tokens ... --tokens-total-source ...
```

## Clean experiment containers

`docker/Dockerfile` builds a clean-room worker image with Claude Code, Codex,
and this CLI preinstalled — no repository checkouts, no host credentials. Each
run injects only its tokens:

```bash
docker build -t scorebench-worker docker/
docker run --rm -it \
  -e HARNESS_URL=https://scorebench.dev/ \
  -e HARNESS_RUN_TOKEN=hrun_... \
  -e CLAUDE_CODE_OAUTH_TOKEN=... \
  -v $HOME/.codex-worker:/home/worker/.codex:ro \
  scorebench-worker
```

- `CLAUDE_CODE_OAUTH_TOKEN` comes from `claude setup-token` on your machine.
- The read-only Codex mount provides a pre-authenticated `auth.json`
  (alternatively inject `OPENAI_API_KEY`).
- No other volumes: the agent cannot see prior attempts or non-public code.
