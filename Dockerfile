FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm
WORKDIR /app
RUN useradd --create-home pulse
COPY --from=build --chown=pulse:pulse /app /app
RUN mkdir -p /app/data && chown pulse:pulse /app/data
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER pulse
EXPOSE 8000
# process type is chosen by the command (see docker-compose.yml)
CMD ["uvicorn", "pulse.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
