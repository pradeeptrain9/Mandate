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
COPY --chown=mandate:mandate docker-entrypoint.sh /app/docker-entrypoint.sh
WORKDIR /app

USER mandate
EXPOSE 8000

# No HEALTHCHECK here, on purpose. One image runs two services with different
# liveness endpoints -- the gateway serves /health and the merchant does not -- so
# a single baked-in probe is wrong for whichever service it was not written for.
# It probed /health and the merchant 404'd on every interval, reporting permanently
# unhealthy while serving traffic perfectly well.
#
# The check belongs where the service is named: compose declares one per service,
# and Render uses its own `healthCheckPath` from render.yaml. Both are per-service
# and neither needs this.

# Which service this container is, chosen by MANDATE_SERVICE rather than by
# overriding the command. An override means every platform that starts this image
# has to tokenise an embedded `sh -c "..."` the same way, and the merchant was the
# only service depending on that.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
