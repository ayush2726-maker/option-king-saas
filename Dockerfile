FROM python:3.13-slim
WORKDIR /app
COPY app_bundle.zip /tmp/app_bundle.zip
RUN python - <<'PY'
import zipfile
with zipfile.ZipFile('/tmp/app_bundle.zip') as archive:
    archive.extractall('/app')
PY
RUN pip install --no-cache-dir -r requirements.txt
ENV PORT=8000
ENV KIRANA_DB_PATH=/data/kirana.db
RUN mkdir -p /data
EXPOSE 8000
CMD ["python", "run.py"]
