FROM python:3.11-slim-bookworm AS model-builder
ENV PIP_NO_CACHE_DIR=1 MODEL_OUTPUT=/models
WORKDIR /build
RUN python -m pip install --upgrade pip \
 && python -m pip install --index-url https://download.pytorch.org/whl/cpu torch==2.6.0 \
 && python -m pip install transformers==4.53.3 onnx==1.17.0 onnxruntime==1.20.1 Pillow==11.3.0 numpy==1.26.4 opencv-python-headless==4.10.0.84
COPY core.py export_models.py ./
RUN python export_models.py


FROM python:3.11-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 \
    OMP_NUM_THREADS=2 ORT_INTRA_THREADS=2
WORKDIR /app

# Dependencias de sistema:
#   libgomp1 -> runtime de OpenMP que usa ONNX Runtime
#   libgl1 / libglib2.0-0 -> por si acaso (OpenCV headless no las necesita, pero no molestan)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
     libgomp1 \
     libgl1 \
     libglib2.0-0 \
     ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements-runtime.txt ./
RUN python -m pip install --upgrade pip \
 && python -m pip install --prefer-binary -r requirements-runtime.txt

COPY --from=model-builder /models ./models
COPY core.py main.py serve.py index.html ./

RUN useradd --uid 10001 --create-home appuser \
 && chown -R appuser:appuser /app
USER appuser
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8080')+'/health',timeout=4)"
CMD ["python", "serve.py"]