# Runs the server over HTTP for remote MCP clients. The vehicle session and the
# issued OAuth grants live in /data, which must be a volume.
FROM python:3.14-slim

ARG VERSION
RUN pip install --no-cache-dir "toyota-mcp${VERSION:+==$VERSION}"

ENV TOYOTA_SESSION_FILE=/data/session.json \
    XDG_STATE_HOME=/data \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8787

ENTRYPOINT ["toyota-mcp"]
CMD ["--http", "http://localhost:8787", "--host", "0.0.0.0"]
