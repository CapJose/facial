# API facial optimizada: FastAPI + ONNX INT8 + Docker

Evalúa cantidad de rostros, posición, inclinación lateral y gafas. No verifica identidad.
El contenedor sirve la cámara en `/`, la API en `/verificar-rostro`, salud en `/health`
y Swagger en `/docs`.

## Ejecutar

Desde esta carpeta:

```sh
docker compose up --build -d
docker compose logs -f api
```

La primera construcción descarga el modelo original, lo exporta a ONNX, cuantiza sus pesos
a INT8 y compara las probabilidades y clases contra PyTorch usando entradas deterministas.
Si la conversión, checksum, firma, etiquetas, preprocesamiento o comparación falla, la imagen
Docker no termina de construirse.

Cuando aparezca `Uvicorn running`, abre directamente:

```text
http://localhost:8080
```

Ya no necesitas ejecutar otro servidor para `index.html` ni configurar CORS en local.
La página abre la cámara, reduce cada fotograma a 720 px, espera cada respuesta y deja 900 ms
antes de enviar el siguiente. Esto evita acumular solicitudes.

Prueba API manual:

```sh
curl -X POST http://localhost:8080/verificar-rostro -F "image=@selfie.jpg"
```

Parar:

```sh
docker compose down
```

Reconstruir desde cero si cambias el modelo o `core.py`:

```sh
docker compose build --no-cache
docker compose up -d
```

## Qué cambió

- La imagen final no instala PyTorch, TorchVision, Transformers ni Hugging Face Hub.
- ONNX Runtime ejecuta el clasificador cuantizado a INT8 en CPU.
- Docker usa varias etapas: PyTorch existe únicamente durante la conversión.
- YuNet tiene checksum SHA-256 verificado.
- El clasificador queda fijado en la revisión `c93c094`.
- ONNX usa dos hilos internos por defecto, ejecución secuencial y optimización completa.
- Un worker y una evaluación simultánea por contenedor evitan duplicar el modelo en RAM.
- Las comprobaciones geométricas se ejecutan antes; si fallan, no se ejecuta el clasificador.
- La imagen analizada por YuNet se limita a 960 px y la cámara envía 720 px.
- Frontend y API comparten origen, eliminando CORS en el uso normal.

La cuantización reduce el modelo y normalmente mejora el uso de CPU, pero la velocidad real
depende de la máquina. Mide latencia y precisión con tus fotografías. La validación de Docker
compara equivalencia técnica con dos imágenes sintéticas; no sustituye una prueba de precisión
con personas, gafas transparentes, oscuras, reflejos e iluminación variada.

## Recursos iniciales

`compose.yaml` asigna 2 CPU y 2 GiB. Son valores iniciales. Revisa `docker stats` y la latencia
mostrada en la interfaz antes de reducir memoria o aumentar tráfico.

Variables:

- `PORT`: 8080.
- `ORT_INTRA_THREADS`: 2. Prueba 1, 2 y 4 con tu CPU; más hilos no siempre reduce latencia.
- `CORS_ORIGINS`: lista separada por comas si alojas el frontend en otro origen.
- `GLASSES_MODEL_PATH`, `GLASSES_META_PATH`, `YUNET_MODEL_PATH`: rutas opcionales.

## Google Cloud Run

```sh
gcloud builds submit --tag REGION-docker.pkg.dev/PROYECTO/REPOSITORIO/rostro-api:onnx --timeout=1800s
gcloud run deploy rostro-api --image REGION-docker.pkg.dev/PROYECTO/REPOSITORIO/rostro-api:onnx --region REGION --port 8080 --cpu 2 --memory 2Gi --concurrency 1 --timeout 120 --max-instances 3
```

Agrega `--allow-unauthenticated` únicamente si quieres una API pública. La cámara puede abrirse
desde la misma URL HTTPS que devuelve Cloud Run.

## AWS

Construye y sube la imagen a ECR:

```sh
docker build --platform linux/amd64 -t rostro-api:onnx .
docker tag rostro-api:onnx CUENTA.dkr.ecr.REGION.amazonaws.com/rostro-api:onnx
docker push CUENTA.dkr.ecr.REGION.amazonaws.com/rostro-api:onnx
```

En App Runner selecciona esa imagen, puerto 8080 y health check `/health`. Usa 2 vCPU,
2 GB y concurrencia baja como punto de partida; ajusta con mediciones.

## Pruebas

Pruebas de API sin descargar pesos:

```sh
python -m pip install -r requirements-test.txt
python -m unittest -v
```

Se verificaron 14 pruebas de transporte y decisiones con un motor simulado, además de sintaxis
de Python/JavaScript y el preprocesamiento liviano. No había Docker en el entorno donde se creó
el proyecto, por lo que debes confirmar la construcción completa con `docker compose up --build`.
