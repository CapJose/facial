import base64
import io
import threading
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image
from main import create_app
from core import interpretar_gafas
from fastapi.testclient import TestClient


class FakeModels:
    def __init__(self):
        self.lock = threading.Lock()
        self.rows = np.array([[150, 130, 200, 220, 200, 210, 300, 210,
                               250, 250, 210, 300, 290, 300, .99]])
        self.result = interpretar_gafas([{'label': 'glasses', 'score': .05},
                                        {'label': 'no%20glasses', 'score': .95}])

    def faces(self, _):
        return self.rows

    def glasses(self, *_):
        return self.result


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = FakeModels()
        self.app = create_app(self.engine)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        buf = io.BytesIO()
        Image.new('RGB', (500, 500), 'white').save(buf, format='JPEG')
        self.raw = buf.getvalue()
        self.data = {'image_base64': base64.b64encode(self.raw).decode()}

    def post(self):
        return self.client.post('/verificar-rostro', json=self.data)

    def test_acceptance_and_file(self):
        self.assertTrue(self.post().json()['apto'])
        result = self.client.post('/verificar-rostro', files={'image': ('photo.jpg', self.raw, 'image/jpeg')})
        self.assertTrue(result.json()['apto'])

    def test_bad_inputs(self):
        for data in ([], None, {}, {'image_base64': 123}, {'image_base64': '!!!!'},
                     {'image_base64': base64.b64encode(b'not an image').decode()}):
            response = self.client.post('/verificar-rostro', json=data)
            self.assertIn(response.status_code, (400, 415))
            self.assertFalse(response.json()['apto'])

    def test_count(self):
        for rows in ([], np.repeat(self.engine.rows, 2, axis=0)):
            self.engine.rows = rows
            self.assertFalse(self.post().json()['apto'])

    def test_uncertain_and_glasses(self):
        for p in (.55, .95):
            self.engine.result = interpretar_gafas([{'label': 'glasses', 'score': p},
                                                   {'label': 'no%20glasses', 'score': 1-p}])
            self.assertFalse(self.post().json()['apto'])

    def test_tilt_and_missing_landmarks(self):
        for eye_y in (260, 400):
            self.engine.rows[0, 7] = eye_y
            self.assertFalse(self.post().json()['apto'])

    def test_busy_and_release(self):
        self.engine.lock.acquire()
        self.assertEqual(self.post().status_code, 503)
        self.engine.lock.release()
        with patch.object(self.engine, 'glasses', side_effect=RuntimeError('test')):
            with self.assertLogs('main', level='ERROR'):
                self.assertEqual(self.post().status_code, 500)
        self.assertTrue(self.post().json()['apto'])

    def test_health_unavailable(self):
        self.app.state.engine = None
        self.assertEqual(self.client.get('/health').status_code, 503)
        self.assertEqual(self.post().status_code, 503)

    def test_unknown_labels(self):
        with self.assertRaises(RuntimeError):
            interpretar_gafas([{'label': 'LABEL_0', 'score': .9}, {'label': 'LABEL_1', 'score': .1}])

    def test_size_and_exif(self):
        with patch('main.MAX_BODY', 100):
            self.assertEqual(self.post().status_code, 413)
        buf = io.BytesIO()
        exif = Image.Exif()
        exif[274] = 6
        Image.new('RGB', (600, 400)).save(buf, format='JPEG', exif=exif)
        self.data['image_base64'] = base64.b64encode(buf.getvalue()).decode()
        self.assertEqual(self.post().json()['detalles']['imagen_original'], {'ancho': 400, 'alto': 600})


    def test_no_content_length_limit(self):
        with patch('main.MAX_BODY', 10):
            response = self.client.post('/verificar-rostro',
                content=iter([b'123456', b'123456']),
                headers={'content-type': 'application/json'})
        self.assertEqual(response.status_code, 413)
        self.assertTrue(self.post().json()['apto'])

    def test_multipart_duplicate_and_malformed(self):
        response = self.client.post('/verificar-rostro', files=[
            ('image', ('a.jpg', self.raw)), ('image', ('b.jpg', self.raw))])
        self.assertEqual(response.status_code, 400)
        response = self.client.post('/verificar-rostro', content=b'abc',
                                   headers={'content-type': 'multipart/form-data'})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(self.post().json()['apto'])

    def test_docs_both_formats(self):
        schema = self.client.get('/openapi.json').json()
        content = schema['paths']['/verificar-rostro']['post']['requestBody']['content']
        self.assertEqual(set(content), {'application/json', 'multipart/form-data'})

    def test_camera_is_served_by_api(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/html', response.headers['content-type'])
        self.assertIn('Iniciar cámara', response.text)

    def test_health_during_inference(self):
        from concurrent.futures import ThreadPoolExecutor
        started, finish = threading.Event(), threading.Event()
        original = self.engine.glasses
        def blocking(*args):
            started.set()
            finish.wait(5)
            return original(*args)
        with patch.object(self.engine, 'glasses', side_effect=blocking):
            with ThreadPoolExecutor() as pool:
                job = pool.submit(self.post)
                try:
                    self.assertTrue(started.wait(2))
                    self.assertEqual(self.client.get('/health').status_code, 200)
                    self.assertEqual(self.post().status_code, 503)
                finally:
                    finish.set()
                self.assertTrue(job.result().json()['apto'])

if __name__ == '__main__':
    unittest.main()
