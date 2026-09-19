"""Motor CPU optimizado: YuNet + clasificador ONNX INT8. No almacena imágenes."""
import io
import json
import logging
import math
import os
import threading
import warnings
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

BASE = Path(__file__).resolve().parent
MODEL_DIR = BASE / 'models'
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_ANALYSIS_SIDE = 960
MIN_FACE_RATIO = 0.15
MAX_FACE_RATIO = 0.65
MAX_OFFSET = 0.20
MAX_TILT_DEGREES = 15.0
GLASSES_THRESHOLD = 0.85
FACE_THRESHOLD = 0.75
FACE_ACCEPT_THRESHOLD = 0.90
LABEL_MAP = {'glasses': True, 'no%20glasses': False}
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
log = logging.getLogger(__name__)


class InputError(Exception):
    pass


def cargar_imagen(raw: bytes):
    if not raw:
        raise InputError('La imagen está vacía.')
    if len(raw) > MAX_FILE_BYTES:
        raise InputError('La imagen supera 8 MiB.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in {'JPEG', 'PNG', 'WEBP'}:
                    raise InputError('Usa una imagen JPEG, PNG o WEBP.')
                if getattr(source, 'n_frames', 1) != 1:
                    raise InputError('Envía una foto estática, sin animación.')
                if source.width * source.height > MAX_PIXELS:
                    raise InputError('La imagen supera 20 megapíxeles.')
                source.load()
                oriented = ImageOps.exif_transpose(source)
                rgba = oriented.convert('RGBA')
                background = Image.new('RGBA', rgba.size, 'white')
                photo = Image.alpha_composite(background, rgba).convert('RGB')
        if min(photo.size) < 160:
            raise InputError('La imagen debe medir al menos 160 píxeles por lado.')
        original_size = photo.size
        photo.thumbnail((MAX_ANALYSIS_SIDE, MAX_ANALYSIS_SIDE), Image.Resampling.LANCZOS)
        return cv2.cvtColor(np.asarray(photo), cv2.COLOR_RGB2BGR), original_size
    except InputError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise InputError('Imagen dañada, incompatible o demasiado grande.') from None


def preprocess_glasses(photo: Image.Image, meta: dict) -> np.ndarray:
    if meta.get('do_resize') is not True or meta.get('do_rescale') is not True or meta.get('do_normalize') is not True:
        raise RuntimeError('Preprocesamiento ONNX no compatible.')
    size = meta.get('size') or {}
    width, height = int(size.get('width', 0)), int(size.get('height', 0))
    if width <= 0 or height <= 0:
        raise RuntimeError('Tamaño de entrada ONNX inválido.')
    photo = photo.convert('RGB').resize((width, height), Image.Resampling(int(meta['resample'])))
    array = np.asarray(photo, dtype=np.float32)
    array *= float(meta['rescale_factor'])
    mean = np.asarray(meta['image_mean'], dtype=np.float32)
    std = np.asarray(meta['image_std'], dtype=np.float32)
    if mean.shape != (3,) or std.shape != (3,) or np.any(std == 0):
        raise RuntimeError('Normalización ONNX inválida.')
    array = (array - mean) / std
    return np.ascontiguousarray(array.transpose(2, 0, 1)[None], dtype=np.float32)


def softmax(logits: np.ndarray) -> np.ndarray:
    values = np.asarray(logits, dtype=np.float64).reshape(-1)
    if values.size != 2 or not np.isfinite(values).all():
        raise RuntimeError('Salida ONNX inválida.')
    values -= values.max()
    exp = np.exp(values)
    return exp / exp.sum()


def interpretar_probabilidades(probabilities, labels):
    if len(probabilities) != 2 or set(labels) != set(LABEL_MAP):
        raise RuntimeError('El modelo devolvió etiquetas inesperadas.')
    scores = {label: float(score) for label, score in zip(labels, probabilities)}
    if any(not math.isfinite(v) or not 0 <= v <= 1 for v in scores.values()):
        raise RuntimeError('Puntuaciones inválidas.')
    top_label = max(scores, key=scores.get)
    confidence = scores[top_label]
    detected = LABEL_MAP[top_label] if confidence >= GLASSES_THRESHOLD else None
    return {'estado': 'incierto' if detected is None else ('con_gafas' if detected else 'sin_gafas'),
            'detectadas': detected, 'etiqueta_modelo': top_label,
            'confianza': round(confidence, 4),
            'puntuaciones': {k: round(v, 4) for k, v in scores.items()},
            'runtime': 'onnx-int8'}


def interpretar_gafas(results):
    labels = [str(item['label']) for item in results]
    return interpretar_probabilidades([float(item['score']) for item in results], labels)


def analizar_posicion(box, width, height):
    x, y, w, h = map(float, box)
    messages = []
    if w / width < MIN_FACE_RATIO:
        messages.append('Acércate un poco más a la cámara.')
    elif w / width > MAX_FACE_RATIO:
        messages.append('Aléjate un poco de la cámara.')
    if abs(x + w / 2 - width / 2) / width > MAX_OFFSET:
        messages.append('Centra tu rostro horizontalmente.')
    if abs(y + h / 2 - height / 2) / height > MAX_OFFSET:
        messages.append('Centra tu rostro verticalmente.')
    margin = max(5, round(min(width, height) * 0.01))
    if x <= margin or y <= margin or x + w >= width - margin or y + h >= height - margin:
        messages.append('Deja un margen alrededor del rostro para que se vea completo.')
    if min(w, h) < 100:
        messages.append('El rostro tiene poca resolución; acércate o mejora la nitidez.')
    return not messages, messages


def analizar_inclinacion(face):
    x, y, w, h = map(float, face[:4])
    eyes = sorted([face[4:6], face[6:8]], key=lambda point: point[0])
    left, right = (np.asarray(point, dtype=float) for point in eyes)
    distance = float(np.linalg.norm(right - left))
    valid = (0.15 * w <= distance <= 0.85 * w and
             all(x <= p[0] <= x + w and y <= p[1] <= y + 0.75 * h for p in eyes))
    if not valid:
        return False, 'No se pudo evaluar la inclinación. Mira de frente.', None
    angle = math.degrees(math.atan2(float(right[1] - left[1]), float(right[0] - left[0])))
    if abs(angle) > MAX_TILT_DEGREES:
        return False, 'Endereza la cabeza; evita inclinarla hacia un lado.', angle
    return True, None, angle


class Models:
    def __init__(self):
        import onnxruntime as ort
        self.lock = threading.Lock()
        cv2.setNumThreads(1)
        yunet_path = Path(os.getenv('YUNET_MODEL_PATH', str(MODEL_DIR / 'yunet.onnx')))
        glasses_path = Path(os.getenv('GLASSES_MODEL_PATH', str(MODEL_DIR / 'glasses-int8.onnx')))
        meta_path = Path(os.getenv('GLASSES_META_PATH', str(MODEL_DIR / 'glasses-meta.json')))
        for path in (yunet_path, glasses_path, meta_path):
            if not path.is_file():
                raise RuntimeError(f'Falta el modelo: {path}')
        self.detector = cv2.FaceDetectorYN.create(str(yunet_path), '', (320, 320), FACE_THRESHOLD, 0.3, 5000)
        self.meta = json.loads(meta_path.read_text(encoding='utf-8'))
        labels = self.meta.get('labels')
        if not isinstance(labels, list) or set(labels) != set(LABEL_MAP):
            raise RuntimeError('Etiquetas del modelo incompatibles.')
        self.labels = labels
        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, int(os.getenv('ORT_INTRA_THREADS', '2')))
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.add_session_config_entry('session.intra_op.allow_spinning', '0')
        self.session = ort.InferenceSession(str(glasses_path), sess_options=options, providers=['CPUExecutionProvider'])
        inputs, outputs = self.session.get_inputs(), self.session.get_outputs()
        if len(inputs) != 1 or len(outputs) != 1:
            raise RuntimeError('Firma ONNX inesperada.')
        self.input_name, self.output_name = inputs[0].name, outputs[0].name
        sample = np.zeros((1, 3, int(self.meta['size']['height']), int(self.meta['size']['width'])), dtype=np.float32)
        softmax(self.session.run([self.output_name], {self.input_name: sample})[0])
        log.info('YuNet y ONNX INT8 listos.')

    def faces(self, bgr):
        height, width = bgr.shape[:2]
        self.detector.setInputSize((width, height))
        _, faces = self.detector.detect(bgr)
        if faces is None:
            return []
        if not np.isfinite(faces).all() or np.any(faces[:, 2:4] <= 0):
            raise RuntimeError('Detección facial inválida.')
        return faces

    def glasses(self, bgr, box):
        height, width = bgr.shape[:2]
        x, y, w, h = map(float, box)
        x1, y1 = max(0, int(x - w * .15)), max(0, int(y - h * .15))
        x2, y2 = min(width, math.ceil(x + w * 1.15)), min(height, math.ceil(y + h * 1.15))
        crop = bgr[y1:y2, x1:x2]
        if crop.size == 0:
            raise RuntimeError('Recorte facial inválido.')
        tensor = preprocess_glasses(Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)), self.meta)
        logits = self.session.run([self.output_name], {self.input_name: tensor})[0]
        return interpretar_probabilidades(softmax(logits), self.labels)


def reply(apto, messages, details=None):
    return {'apto': bool(apto), 'mensajes': messages, 'detalles': details or {}}


def evaluar(engine, raw):
    bgr, original_size = cargar_imagen(raw)
    height, width = bgr.shape[:2]
    faces = engine.faces(bgr)
    details = {'rostros_detectados': len(faces),
               'imagen_original': {'ancho': original_size[0], 'alto': original_size[1]},
               'imagen_analizada': {'ancho': width, 'alto': height},
               'coordenadas_bbox': 'imagen_analizada_con_orientacion_EXIF_corregida'}
    if len(faces) != 1:
        message = ('No se detectó ningún rostro. Mira de frente y mejora la iluminación.' if len(faces) == 0
                   else f'Se detectaron {len(faces)} rostros. Debe aparecer una sola persona.')
        return reply(False, [message], details)
    face = faces[0]
    details['bbox'] = dict(zip(('x', 'y', 'ancho', 'alto'), (round(float(v), 2) for v in face[:4])))
    details['confianza_rostro'] = round(float(face[-1]), 4)
    if float(face[-1]) < FACE_ACCEPT_THRESHOLD:
        return reply(False, ['La detección del rostro es incierta. Mira de frente.'], details)
    position_ok, messages = analizar_posicion(face[:4], width, height)
    tilt_ok, tilt_message, angle = analizar_inclinacion(face)
    if tilt_message:
        messages.append(tilt_message)
    details.update(posicion_ok=position_ok, inclinacion_ok=tilt_ok,
                   angulo_inclinacion=round(angle, 2) if angle is not None else None)
    if not position_ok or not tilt_ok:
        details['gafas'] = {'estado': 'no_evaluado', 'detectadas': None}
        return reply(False, messages, details)
    glasses = engine.glasses(bgr, face[:4])
    details['gafas'] = glasses
    if glasses['detectadas'] is None:
        return reply(False, ['No se pudo determinar si llevas gafas. Mejora la iluminación.'], details)
    if glasses['detectadas']:
        return reply(False, ['Quítate las gafas para continuar.'], details)
    return reply(True, ['Todo correcto: rostro bien posicionado, recto y sin gafas.'], details)


# ============================================================
# MOTOR: DETECCIÓN DE DOCUMENTO DE IDENTIDAD
# ============================================================
DOC_ASPECT_RATIOS = (1.586, 1.500, 1.420, 1.350, 0.65, 0.70)
DOC_ASPECT_TOLERANCE = 0.18
DOC_MIN_AREA_RATIO = 0.18
DOC_MAX_AREA_RATIO = 0.98
DOC_MIN_SHARPNESS = 35.0
DOC_MIN_SIDE_PX = 180
DOC_MIN_TEXT_CONTRAST = 25.0
DOC_BLUR_KERNEL = (5, 5)

DOC_MODEL_PATH = Path(os.getenv('DOC_MODEL_PATH', str(MODEL_DIR / 'documento-int8.onnx')))
DOC_META_PATH = Path(os.getenv('DOC_META_PATH', str(MODEL_DIR / 'documento-meta.json')))
DOC_LABEL_MAP = {'document': True, 'no%20document': False, 'documento': True, 'no_documento': False}
DOC_THRESHOLD = 0.80


def _ordenar_quad(points: np.ndarray) -> np.ndarray:
    pts = points.reshape(4, 2).astype(np.float32)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([
        pts[np.argmin(s)],
        pts[np.argmin(d)],
        pts[np.argmax(s)],
        pts[np.argmax(d)],
    ], dtype=np.float32)


def _ratio_documento(quad: np.ndarray):
    tl, tr, br, bl = quad
    ancho = (np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / 2.0
    alto = (np.linalg.norm(bl - tl) + np.linalg.norm(br - tr)) / 2.0
    if alto <= 0:
        return 0.0, 0.0, 0.0
    return ancho, alto, ancho / alto


def _aspecto_valido(ratio: float) -> bool:
    for objetivo in DOC_ASPECT_RATIOS:
        if abs(ratio - objetivo) / objetivo <= DOC_ASPECT_TOLERANCE:
            return True
        inv = 1.0 / objetivo
        if abs(ratio - inv) / inv <= DOC_ASPECT_TOLERANCE:
            return True
    return False


def _nitidez(gray: np.ndarray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _contraste_texto(gray: np.ndarray) -> float:
    h, w = gray.shape[:2]
    y1, y2 = int(h * 0.20), int(h * 0.80)
    x1, x2 = int(w * 0.20), int(w * 0.80)
    zona = gray[y1:y2, x1:x2]
    return float(zona.std()) if zona.size else 0.0


def _cargar_clasificador_documento():
    import onnxruntime as ort
    meta = json.loads(DOC_META_PATH.read_text(encoding='utf-8'))
    labels = meta.get('labels')
    if not isinstance(labels, list) or not labels:
        raise RuntimeError('Etiquetas de documento inválidas.')
    options = ort.SessionOptions()
    options.intra_op_num_threads = max(1, int(os.getenv('ORT_INTRA_THREADS', '2')))
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    session = ort.InferenceSession(
        str(DOC_MODEL_PATH), sess_options=options,
        providers=['CPUExecutionProvider']
    )
    inputs, outputs = session.get_inputs(), session.get_outputs()
    if len(inputs) != 1 or len(outputs) != 1:
        raise RuntimeError('Firma ONNX de documento inesperada.')
    sample = np.zeros(
        (1, 3, int(meta['size']['height']), int(meta['size']['width'])),
        dtype=np.float32
    )
    session.run([outputs[0].name], {inputs[0].name: sample})
    analizar_documento._session = session
    analizar_documento._meta = meta
    analizar_documento._labels = labels
    analizar_documento._input_name = inputs[0].name
    analizar_documento._output_name = outputs[0].name
    log.info('Clasificador de documento ONNX listo.')


def analizar_documento(bgr: np.ndarray):
    h, w = bgr.shape[:2]
    resultado = {
        'detectado': False, 'quad': None, 'bbox': None,
        'ratio': 0.0, 'area_ratio': 0.0,
        'nitidez': 0.0, 'contraste': 0.0,
        'mensajes': [], 'motivos': [],
    }

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, DOC_BLUR_KERNEL, 0)

    edges = cv2.Canny(gray, 40, 120)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)

    contornos, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contornos:
        resultado['mensajes'].append('No se detectó ningún documento. Colócalo sobre una superficie plana y con buen contraste.')
        resultado['motivos'].append('sin_contornos')
        return resultado

    area_frame = float(w * h)
    candidatos = []
    for c in contornos:
        area = cv2.contourArea(c)
        if area < area_frame * 0.05:
            continue
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            quad = _ordenar_quad(approx)
            ancho, alto, ratio = _ratio_documento(quad)
            if ancho < DOC_MIN_SIDE_PX or alto < DOC_MIN_SIDE_PX:
                continue
            area_ratio = area / area_frame
            candidatos.append({
                'quad': quad, 'area': area, 'ratio': ratio,
                'ancho': ancho, 'alto': alto, 'area_ratio': area_ratio,
            })

    if not candidatos:
        resultado['mensajes'].append('No se reconoció la forma del documento. Enmárcalo completo y evita fondos con patrones.')
        resultado['motivos'].append('sin_quad')
        return resultado

    candidatos.sort(key=lambda c: c['area'], reverse=True)
    mejor = candidatos[0]
    quad = mejor['quad']
    xs, ys = quad[:, 0], quad[:, 1]
    x1, y1 = int(max(0, xs.min())), int(max(0, ys.min()))
    x2, y2 = int(min(w, xs.max())), int(min(h, ys.max()))
    bbox = (x1, y1, x2 - x1, y2 - y1)

    resultado.update({
        'quad': quad.tolist(),
        'bbox': bbox,
        'ratio': round(mejor['ratio'], 3),
        'area_ratio': round(mejor['area_ratio'], 4),
    })

    if not _aspecto_valido(mejor['ratio']):
        resultado['mensajes'].append('La forma detectada no corresponde a un documento de identidad. Muéstralo completo y sin tapar bordes.')
        resultado['motivos'].append('aspecto_invalido')
        return resultado

    if mejor['area_ratio'] < DOC_MIN_AREA_RATIO:
        resultado['mensajes'].append('Acerca el documento a la cámara para que ocupe más espacio.')
        resultado['motivos'].append('area_pequena')
        return resultado
    if mejor['area_ratio'] > DOC_MAX_AREA_RATIO:
        resultado['mensajes'].append('El documento está demasiado cerca o cortado. Aléjalo un poco.')
        resultado['motivos'].append('area_grande')
        return resultado

    roi = bgr[y1:y2, x1:x2]
    if roi.size == 0:
        resultado['mensajes'].append('Recorte inválido del documento.')
        resultado['motivos'].append('roi_vacio')
        return resultado

    gray_roi = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    nitidez = _nitidez(gray_roi)
    contraste = _contraste_texto(gray_roi)
    resultado['nitidez'] = round(nitidez, 2)
    resultado['contraste'] = round(contraste, 2)

    if nitidez < DOC_MIN_SHARPNESS:
        resultado['mensajes'].append('La imagen está borrosa. Mantén el pulso firme y enfoca el documento.')
        resultado['motivos'].append('borroso')
        return resultado
    if contraste < DOC_MIN_TEXT_CONTRAST:
        resultado['mensajes'].append('No se distingue el contenido del documento. Mejora la iluminación.')
        resultado['motivos'].append('bajo_contraste')
        return resultado

    if DOC_MODEL_PATH.is_file() and DOC_META_PATH.is_file():
        try:
            if not hasattr(analizar_documento, '_session'):
                _cargar_clasificador_documento()
            session = analizar_documento._session
            meta = analizar_documento._meta
            labels = analizar_documento._labels
            input_name = analizar_documento._input_name
            output_name = analizar_documento._output_name
            crop_rgb = cv2.cvtColor(roi, cv2.COLOR_BGR2RGB)
            tensor = preprocess_glasses(Image.fromarray(crop_rgb), meta)
            logits = session.run([output_name], {input_name: tensor})[0]
            probs = softmax(logits)
            scores = {lab: float(p) for lab, p in zip(labels, probs)}
            top = max(scores, key=scores.get)
            conf = scores[top]
            resultado['clasificador'] = {
                'etiqueta': top,
                'confianza': round(conf, 4),
                'puntuaciones': {k: round(v, 4) for k, v in scores.items()},
            }
            es_doc = DOC_LABEL_MAP.get(top)
            if es_doc is False and conf >= DOC_THRESHOLD:
                resultado['mensajes'].append('Lo detectado no parece un documento de identidad válido.')
                resultado['motivos'].append('clasificador_rechaza')
                return resultado
            if es_doc is None and conf >= DOC_THRESHOLD:
                resultado['mensajes'].append('No se pudo confirmar que sea un documento de identidad.')
                resultado['motivos'].append('clasificador_incierto')
                return resultado
        except Exception as e:
            log.warning(f'Clasificador de documento falló: {e}')

    resultado['detectado'] = True
    resultado['mensajes'].append('Documento de identidad detectado correctamente.')
    return resultado


def evaluar_documento(engine, raw, esperado: str = "documento"):
    """
    Verifica que en la imagen haya un documento de identidad.
    `engine` puede ser None (no usa modelos ONNX, solo OpenCV).
    `esperado`: 'documento' | 'dni' | 'pasaporte' (informativo).
    """
    bgr, original_size = cargar_imagen(raw)
    h, w = bgr.shape[:2]
    info = analizar_documento(bgr)
    details = {
        'imagen_original': {'ancho': original_size[0], 'alto': original_size[1]},
        'imagen_analizada': {'ancho': w, 'alto': h},
        'tipo_esperado': esperado,
        'documento': {
            'detectado': info['detectado'],
            'ratio': info['ratio'],
            'area_ratio': info['area_ratio'],
            'nitidez': info['nitidez'],
            'contraste': info['contraste'],
            'motivos': info['motivos'],
        },
    }
    if info.get('bbox'):
        details['documento']['bbox'] = dict(zip(('x', 'y', 'ancho', 'alto'), info['bbox']))
    if info.get('clasificador'):
        details['documento']['clasificador'] = info['clasificador']

    if not info['detectado']:
        mensajes = info['mensajes'] or ['No se pudo verificar el documento.']
        return reply(False, mensajes, details)
    return reply(True, ['Documento de identidad verificado correctamente.'], details)