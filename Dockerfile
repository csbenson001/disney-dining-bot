FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y xvfb tzdata && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install chromium

COPY . .

ENV DISNEY_BROWSER_PROFILE_DIR=/data/browser-profile
ENV DISNEY_HEADLESS=false
ENV PYTHONUNBUFFERED=1

CMD ["bash", "run_bot.sh"]
