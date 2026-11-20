FROM python:3.15-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LOMBO_TIER_DATA_DIR=/data
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
# /data holds the state file and the HDD file list. It is world-writable (sticky) so a fresh volume works
# for whatever user the container is told to run as: it must be the user that owns the media files.
RUN pip install --no-cache-dir . \
    && useradd --system --no-create-home app \
    && mkdir /data && chmod 1777 /data
VOLUME /data
USER app
ENTRYPOINT ["lombo-tier"]
CMD ["run", "--loop"]
