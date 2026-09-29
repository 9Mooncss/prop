#!/bin/sh
set -eu
case "${1:-api}" in
  api)
    propguard db upgrade
    if [ "${PROPGUARD_SEED_ON_START:-true}" = "true" ]; then propguard seed load --dir /app/seed/firms >/dev/null; fi
    exec propguard serve --host 0.0.0.0 --port 8000 ;;
  worker)
    # wait until the API container has applied migrations
    i=0; until propguard db current 2>/dev/null | grep -q '^[0-9]'; do i=$((i+1)); [ $i -gt 60 ] && exit 1; sleep 2; done
    exec propguard worker ;;
  *) exec propguard "$@" ;;
esac
