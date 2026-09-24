import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from wellflow.app.services.image_thumbnail import thumbnail_path
from wellflow.app.api import tasks, uploads


class TaskPresentationTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_response_keeps_urls_and_review_without_mutating_checkpoint(self):
        state = {
            'request': {'product_images': ['uploads/task/product.png']},
            'node1': {'product_insight': 'report', 'report_sections': {}, 'compressed_images': ['data:image/png;base64,large']},
            'node3': {'generate_prompts': ['prompt'], 'compressed_model_images': ['data:image/png;base64,large']},
            'node4': {'reference_images': ['data:image/png;base64,large'],
                      'reference_images_data_uris': ['data:image/png;base64,large'],
                      'outputs': [{'image_url': '/uploads/task/result.png', 'work_item_id': 'shot-01'}]},
        }
        before = copy.deepcopy(state)
        task = SimpleNamespace(task_id='task', phase='c4_review', request_json={},
                               selected_plan_ids_json=[], created_at=datetime.now(timezone.utc),
                               updated_at=datetime.now(timezone.utc))
        with patch.object(tasks, 'TaskRepo') as repo, patch.object(tasks, '_aget_graph_state', AsyncMock(return_value=(state, SimpleNamespace(next=())))), \
             patch('wellflow.app.workflow_status.checkpoint_view', return_value=('c4_review', {'node': 'c4', 'revision': 'rev'})), \
             patch('wellflow.app.graph_context.check_graph_runtime_state', return_value=('interrupted', None)), \
             patch('wellflow.app.event_bus.is_running', return_value=False), \
             patch('wellflow.app.generation_journal.restore_completed', side_effect=lambda task_id, node, rev: node):
            repo.return_value.get.return_value = task
            repo.return_value.list_images.return_value = []
            result = (await tasks.get_task('task', object()))['data']
        self.assertNotIn('compressed_images', result.node1)
        self.assertNotIn('compressed_model_images', result.node3)
        self.assertNotIn('reference_images', result.node4)
        self.assertNotIn('reference_images_data_uris', result.node4)
        self.assertEqual(result.request['product_images'], ['uploads/task/product.png'])
        self.assertEqual(result.node4['outputs'][0]['image_url'], '/uploads/task/result.png')
        self.assertEqual(result.interrupt['revision'], 'rev')
        self.assertEqual(state, before)


class ThumbnailTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.source = self.root / 'uploads'
        self.source.mkdir()
        self.cache = self.root / 'cache'
        self.roots = {'/uploads/': self.source}
        Image.new('RGB', (1200, 600), 'red').save(self.source / 'image.png')

    def test_thumbnail_resizes_caches_and_preserves_original(self):
        original = (self.source / 'image.png').read_bytes()
        target = thumbnail_path('/uploads/image.png', 160, self.roots, self.cache)
        with Image.open(target) as image:
            self.assertEqual(image.size, (160, 80))
            self.assertEqual(image.format, 'WEBP')
        first = target.stat().st_mtime_ns
        self.assertEqual(thumbnail_path('/uploads/image.png', 160, self.roots, self.cache), target)
        self.assertEqual(target.stat().st_mtime_ns, first)
        self.assertEqual((self.source / 'image.png').read_bytes(), original)
        Image.new('RGB', (300, 600), 'blue').save(self.source / 'image.png')
        self.assertNotEqual(thumbnail_path('/uploads/image.png', 160, self.roots, self.cache), target)

    def test_outside_paths_remote_urls_and_symlinks_are_rejected(self):
        (self.root / 'secret.png').write_bytes((self.source / 'image.png').read_bytes())
        (self.source / 'link.png').symlink_to(self.root / 'secret.png')
        for source in ['/uploads/../secret.png', '/uploads/link.png', 'https://example.com/image.png', '/uploads/missing.png']:
            with self.subTest(source=source), self.assertRaises(HTTPException) as raised:
                thumbnail_path(source, 160, self.roots, self.cache)
            self.assertEqual(raised.exception.status_code, 404)

    def test_route_returns_cacheable_image_and_validates_size(self):
        app = FastAPI()
        app.include_router(uploads.router, prefix='/api')
        with patch.object(uploads.settings, 'upload_dir', str(self.source)), TestClient(app) as client:
            response = client.get('/api/wellflow/image/thumbnail', params={'src': '/uploads/image.png', 'size': 160})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['content-type'], 'image/webp')
            self.assertIn('max-age', response.headers['cache-control'])
            self.assertEqual(client.get('/api/wellflow/image/thumbnail', params={'src': '/uploads/image.png', 'size': 9999}).status_code, 422)


if __name__ == '__main__':
    unittest.main()
