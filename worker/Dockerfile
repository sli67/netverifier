# Build from the worker folder: docker build worker
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY verifier_worker verifier_worker
RUN uv sync --frozen --no-dev
# Lets DATABASE_URL verify the RDS certificate (sslrootcert=/etc/ssl/rds-global-bundle.pem)
ADD https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /etc/ssl/rds-global-bundle.pem
RUN chmod 644 /etc/ssl/rds-global-bundle.pem
ENV PATH=/app/.venv/bin:$PATH HOME=/tmp
USER nobody
CMD ["python", "-m", "verifier_worker"]
