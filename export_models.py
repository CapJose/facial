"""Descarga, exporta, cuantiza y valida los modelos durante docker build."""
import hashlib
import json
import os
import urllib.request
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort
import torch
from onnxruntime.quantization import QuantType, quantize_dynamic
from PIL import Image
from transformers import AutoImageProcessor, AutoModelForImageClassification

from core import LABEL_MAP, preprocess_glasses, softmax

OUT = Path(os.environ.get('MODEL_OUTPUT', '/models'))
REPO = 'youngp5/eyeglasses_detection'
REVISION = os.environ.get('GLASSES_REVISION', 'c93c094')
YUNET_URL = ('https://media.githubusercontent.com/media/opencv/opencv_zoo/'
             'main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx')
YUNET_SHA256 = '8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4'
OUT.mkdir(parents=True, exist_ok=True)

print('Descargando y verificando YuNet...')
with urllib.request.urlopen(YUNET_URL, timeout=90) as response:
    yunet = response.read(1_000_000)
if hashlib.sha256(yunet).hexdigest() != YUNET_SHA256:
    raise RuntimeError('El checksum de YuNet no coincide.')
(OUT / 'yunet.onnx').write_bytes(yunet)

print('Descargando clasificador fijado en la revisión', REVISION)
processor = AutoImageProcessor.from_pretrained(REPO, revision=REVISION)
model = AutoModelForImageClassification.from_pretrained(REPO, revision=REVISION)
model.eval()
labels = [str(model.config.id2label[i]) for i in range(model.config.num_labels)]
if set(labels) != set(LABEL_MAP) or len(labels) != 2:
    raise RuntimeError(f'Etiquetas incompatibles: {labels}')
meta = {
    'labels': labels,
    'do_resize': bool(processor.do_resize),
    'do_rescale': bool(processor.do_rescale),
    'do_normalize': bool(processor.do_normalize),
    'size': {'height': int(processor.size['height']), 'width': int(processor.size['width'])},
    'resample': int(processor.resample),
    'rescale_factor': float(processor.rescale_factor),
    'image_mean': list(map(float, processor.image_mean)),
    'image_std': list(map(float, processor.image_std)),
    'source': REPO,
    'revision': REVISION,
}
(OUT / 'glasses-meta.json').write_text(json.dumps(meta, indent=2), encoding='utf-8')

# Comprueba que el preprocesamiento liviano sea equivalente al oficial.
rng = np.random.default_rng(20260913)
photos = [Image.fromarray(rng.integers(0, 256, (240, 320, 3), dtype=np.uint8)),
          Image.new('RGB', (300, 200), (32, 128, 224))]
for photo in photos:
    official = processor(images=photo, return_tensors='np')['pixel_values']
    lightweight = preprocess_glasses(photo, meta)
    if not np.allclose(official, lightweight, atol=1e-6):
        raise RuntimeError('El preprocesamiento liviano no coincide con Transformers.')

float_path = OUT / 'glasses-fp32.onnx'
int8_path = OUT / 'glasses-int8.onnx'
dummy = torch.zeros(1, 3, meta['size']['height'], meta['size']['width'], dtype=torch.float32)
print('Exportando ONNX FP32...')
with torch.inference_mode():
    torch.onnx.export(model, (dummy,), str(float_path), input_names=['pixel_values'],
                      output_names=['logits'], dynamic_axes={'pixel_values': {0: 'batch'}, 'logits': {0: 'batch'}},
                      opset_version=17, do_constant_folding=True, dynamo=False)
onnx.checker.check_model(onnx.load(str(float_path)))
print('Cuantizando pesos MatMul/Gemm a INT8...')
quantize_dynamic(str(float_path), str(int8_path), weight_type=QuantType.QInt8,
                 op_types_to_quantize=['MatMul', 'Gemm'])
onnx.checker.check_model(onnx.load(str(int8_path)))

# Compara probabilidades y decisión contra PyTorch en entradas deterministas.
session = ort.InferenceSession(str(int8_path), providers=['CPUExecutionProvider'])
max_delta = 0.0
with torch.inference_mode():
    for photo in photos:
        pixels = preprocess_glasses(photo, meta)
        torch_logits = model(pixel_values=torch.from_numpy(pixels)).logits.numpy()
        onnx_logits = session.run(['logits'], {'pixel_values': pixels})[0]
        torch_probs, onnx_probs = softmax(torch_logits), softmax(onnx_logits)
        max_delta = max(max_delta, float(np.max(np.abs(torch_probs - onnx_probs))))
        if int(torch_probs.argmax()) != int(onnx_probs.argmax()):
            raise RuntimeError('INT8 cambió la clase ganadora en la validación técnica.')
if max_delta > 0.05:
    raise RuntimeError(f'INT8 se alejó demasiado de FP32: {max_delta:.6f}')
float_path.unlink()
print(f'Modelos listos. Diferencia máxima de probabilidad INT8/FP32: {max_delta:.6f}')
