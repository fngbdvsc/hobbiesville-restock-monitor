FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
ENV PYTHONUNBUFFERED=1 HEADLESS=0
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY hobbiesville_monitor.py .
# Chrome runs as a normal window on a virtual screen (Xvfb). xvfb-run hung in this container on the other bots.
CMD ["sh", "-c", "Xvfb :99 -screen 0 1366x768x24 -nolisten tcp & sleep 2; DISPLAY=:99 exec python hobbiesville_monitor.py"]
