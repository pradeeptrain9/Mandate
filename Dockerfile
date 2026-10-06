# One image, two services. The gateway and the merchant stub run as separate
# containers from the same build because they are separate trust domains -- the
# merchant is an untrusted third party that signs quotes, and putting them in one
# process would make it too easy to hand the gateway something an outside caller
# could not reach. Compose gives them different commands, not different images.

FROM python:3.12-slim AS build

# Wheels are built here and copied forward, so the runtime stage carries no
# compiler and no build cache.
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install --upgrade pip build \
 && python -m pip wheel --wheel-dir /wheels .


FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    MANDATE_VAR_DIR=/var/lib/mandate

# Not root. The gateway holds an HMAC key and talks to PayPal; there is no reason
# for it to be able to write to its own code.
RUN useradd --create-home --uid 10001 mandate

COPY --from=build /wheels /wheels
RUN python -m pip install --no-index --find-links=/wheels mandate \
 && rm -rf /wheels

# The ledger and the hold database live here. Declared as a volume in compose and
# as a disk on Render, because losing it means losing the audit log.
RUN install -d -o mandate -g mandate /var/lib/mandate
VOLUME ["/var/lib/mandate"]

# Scripts are not part of the installed package -- they are operator tools, and
# the seed script is one of them.
COPY --chown=mandate:mandate scripts /app/scripts
WORKDIR /app

USER mandate
EXPOSE 8000

# No curl in slim, and adding it to run a health check would be a larger attack
# surface than the check is worth.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
  CMD python -c "import os,urllib.request,sys; \
port=os.environ.get('PORT','8000'); \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=2).status == 200 else 1)"

# Overridden by compose for the merchant. uvicorn binds 0.0.0.0 inside a container
# on purpose: the port is published deliberately or not at all.
CMD ["sh", "-c", "exec uvicorn mandate.gateway.api:create_app --factory --host 0.0.0.0 --port ${PORT:-8000}"]
