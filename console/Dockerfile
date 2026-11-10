# Build context is the repository root (see azure.yaml), so the console reuses the agent presets,
# the handbook and the eval helpers of the CLI demo.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY console/requirements.txt console/requirements.txt
RUN pip install --no-cache-dir -r console/requirements.txt

COPY agent/common.py agent/common.py
COPY agent/instructions agent/instructions
COPY agent/knowledge agent/knowledge
COPY demo/evalkit.py demo/evalkit.py
COPY console/app console/app

RUN useradd --create-home --uid 10001 console
USER console

EXPOSE 8000
CMD ["uvicorn", "main:app", "--app-dir", "console/app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
