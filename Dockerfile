FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 TZ=America/Sao_Paulo
COPY pyproject.toml README.md ./
COPY mesa ./mesa
RUN pip install --no-cache-dir . && mkdir -p /app/dados
EXPOSE 8100
CMD ["python", "-m", "mesa"]
