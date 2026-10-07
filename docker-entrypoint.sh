#!/bin/sh
# Pick the service from an environment variable instead of a command override.
#
# Both services run from one image, so something has to say which. That used to be
# a `dockerCommand` in render.yaml carrying an embedded `sh -c "..."` with quotes
# and a `$PORT` to expand -- which makes the service depend on how one particular
# platform tokenises a command string, and the merchant was the only service that
# relied on it. An environment variable has no quoting to get wrong.
#
# $PORT is expanded here, by a real shell, which is the one place it is guaranteed
# to be a shell. Defaults to 8000 so `docker run` with no PORT still works.
set -e

PORT="${PORT:-8000}"

case "${MANDATE_SERVICE:-gateway}" in
  gateway)
    exec uvicorn mandate.gateway.api:create_app --factory --host 0.0.0.0 --port "$PORT"
    ;;
  merchant)
    exec uvicorn mandate.merchant.app:app --host 0.0.0.0 --port "$PORT"
    ;;
  proxy)
    # The compromised tool server. Never started unless asked for by name.
    exec uvicorn mandate.demo.hostile_proxy:build --factory --host 0.0.0.0 --port "$PORT"
    ;;
  *)
    echo "MANDATE_SERVICE must be gateway, merchant or proxy; got '${MANDATE_SERVICE}'" >&2
    exit 2
    ;;
esac
