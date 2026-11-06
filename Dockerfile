FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY proxy.py .

# Run unprivileged (port > 1024, so no root needed)
USER nobody

CMD ["python", "proxy.py"]
