# Build from the repository root (the image contains shared/, which the desktop edition uses too):
#   docker build -f server/Dockerfile -t debrief-ai .          (docker compose does this for you)
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
# The Claude Code CLI and the GitHub Copilot CLI (each user's own login for the model they picked runs through them).
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
    && npm install -g @anthropic-ai/claude-code @github/copilot && apt-get purge -y npm && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /root/.npm
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY server/backend/requirements.txt ./
RUN pip install --default-timeout=300 --retries 10 -r requirements.txt
# Speaker-recognition model (26 MB), baked in so the server works offline.
RUN mkdir -p /app/models && python -c "import urllib.request as u; u.urlretrieve('https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx', '/app/models/wespeaker_en_voxceleb_resnet34_LM.onnx')"
COPY server/backend/ ./
COPY server/frontend/ ./frontend/
COPY shared/ ./shared/
RUN mkdir -p /data /home/app/.cache/huggingface /home/app/.claude /home/app/.copilot && chown -R app:app /data /app /home/app
USER app
ENV HOME=/home/app DATA_DIR=/data ENV=prod PYTHONPATH=/app SPEAKER_MODEL=/app/models/wespeaker_en_voxceleb_resnet34_LM.onnx
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
# Single worker on purpose: live audio segmenters and the session loops live in this process.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers"]
