FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY scripts ./scripts
RUN python -m pip install --no-cache-dir .
COPY config ./config
COPY ui/control ./ui/control
RUN useradd --create-home recovery && mkdir -p data/console data/live && chown -R recovery:recovery data
USER recovery
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "src.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
