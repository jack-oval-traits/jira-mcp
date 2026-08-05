# Token-lean Jira MCP adapter.
#
# Single stage: the app is a few hundred lines of Python with no build step, so a
# multi-stage build would only save the ~20MB uv binary.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# One install, after the source is in place. Splitting deps into an earlier layer
# needs the project itself to be installable at that point, which it is not --
# and uv resolves this dependency set in a couple of seconds anyway.
#
# LICENSE is copied because pyproject sets license-files; the wheel build fails
# without it. `[measure]` pulls in tiktoken so scripts/token_compare.py runs in
# the container without a second install -- ~10MB, worth it for the one script.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY scripts ./scripts
RUN uv pip install --system --no-cache ".[measure]"

# The app defaults to a loopback bind (it holds a Jira API token). In a
# container that would be unreachable, and the port is only exposed if Docker is
# told to publish it -- compose.yaml publishes to 127.0.0.1 by default.
ENV JIRA_MCP_LISTEN=0.0.0.0

# Non-root: this container holds a Jira API token and needs no write access.
RUN useradd --system --uid 10001 jira
USER jira

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8787/healthz', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "jira_mcp.server"]
