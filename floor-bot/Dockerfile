FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# Install system dependencies (pulseaudio/ffmpeg)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    pulseaudio \
    dbus \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY bot.py .

CMD ["sh", "-c", "pip install --no-cache-dir playwright==1.40.0 && python bot.py"]
