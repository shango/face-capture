#!/bin/sh
set -eu
# --proxy-headers: trust X-Forwarded-* from Railway's proxy so access logs and
# request.client reflect the real client. Rate limiting derives the client IP
# from X-Forwarded-For directly (see app/ratelimit.py), independent of this.
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers
