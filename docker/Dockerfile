# Clean-room ScoreBench worker: agent CLIs only, no private code, no history.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git ripgrep tmux \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code @openai/codex \
    && pip install --no-cache-dir git+https://github.com/josusanmartin/scorebench-cli

RUN useradd --create-home --shell /bin/bash worker
USER worker
WORKDIR /work

# Injected at run time, never baked in:
#   HARNESS_URL, HARNESS_RUN_TOKEN        scoped exercise API key
#   CLAUDE_CODE_OAUTH_TOKEN               from `claude setup-token`
#   OPENAI_API_KEY or ~/.codex mount      Codex auth
CMD ["/bin/bash"]
