FROM python:3.14-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py config.yaml ./
RUN mkdir -p recordings clips logs review

EXPOSE 8765

# EXTRA_ARGS defaults to --no-publish; set it to an empty string to enable
# publishing once SupoClip credentials are configured.
ENV EXTRA_ARGS=--no-publish
CMD ["sh", "-c", "exec python -u supervisor.py $EXTRA_ARGS"]
