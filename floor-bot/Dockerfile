FROM mcr.microsoft.com/playwright/python:v1.65.2-jammy

# The Ubuntu mirrors can be unreachable over HTTP from CI builders.
# Fail on incomplete indexes rather than trying to install from a partial update.
RUN sed -i -E 's@http://((archive|security|ports)\.ubuntu\.com/)@https://\1@g' /etc/apt/sources.list \
    && apt-get -o Acquire::Retries=3 -o Acquire::https::Timeout=30 update --error-on=any \
    && apt-get -o Acquire::Retries=3 -o Acquire::https::Timeout=30 install -y \
    ffmpeg \
    pulseaudio \
    dbus \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY bot.py .

CMD ["sh", "-c", "pip install --no-cache-dir playwright==1.40.0 && python bot.py"]
