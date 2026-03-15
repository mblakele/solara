import unittest
import json
from app import app
from metrics import MetricsMock

class TestApp(unittest.TestCase):
    def setUp(self):
        self.app = app.test_client()
        self.app.testing = True

    def test_health_endpoint(self):
        response = self.app.get('/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.decode('utf-8'), 'ok')
        self.assertEqual(response.headers['Content-Type'], 'text/plain')

    def test_index_json_mock(self):
        # By default, VUE_USERNAME is None in this environment, so it uses MetricsMock
        response = self.app.get('/', headers={'Accept': 'application/json'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Content-Type'], 'application/json')
        
        data = json.loads(response.data)
        self.assertIn('devices', data)
        self.assertTrue(len(data['devices']) > 0)
        # We've seen different values for GID and Name in the mock, 
        # so we'll just verify the keys exist in the first device.
        device = data['devices'][0]
        self.assertIn('gid', device)
        self.assertIn('name', device)
        self.assertIn('prediction', device)

    def test_index_html_mock(self):
        response = self.app.get('/', headers={'Accept': 'text/html'})
        self.assertEqual(response.status_code, 200)
        # The word 'MOCK' might not be in the HTML if it's using the values directly.
        # Let's check for some characteristic HTML instead.
        self.assertIn(b'Wh, predicted total', response.data)

    def test_not_acceptable(self):
        response = self.app.get('/', headers={'Accept': 'text/plain'})
        self.assertEqual(response.status_code, 406)

if __name__ == '__main__':
    unittest.main()
