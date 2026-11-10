# Program relay media plane (#749). Stdlib Python + FFmpeg only; no portal code or secrets baked in.
FROM python:3.13-slim

# ca-certificates: the relay verifies the YouTube RTMPS certificate (relay.py tls_options).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY relay.py .

# Unprivileged: the relay only needs outbound RTSP (MediaMTX) and RTMPS (YouTube).
USER nobody
EXPOSE 8090
CMD ["python", "-u", "relay.py"]
