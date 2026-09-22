"""预上传参考图的去重持久化。"""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from wellflow.app.repositories.task_repo import TaskRepo


class ReferenceImagePersistenceTest(unittest.TestCase):
    def test_saves_preuploaded_paths_without_duplicates(self):
        repo = object.__new__(TaskRepo)
        repo.list_images = Mock(return_value=[
            SimpleNamespace(image_type="mannequin", storage_uri="uploads/old.png"),
        ])
        repo.save_images = Mock()

        repo.save_reference_images("task-1", {
            "mannequin": ["uploads/old.png", "uploads/new.png", "uploads/new.png"],
            "scene": ["uploads/scene.png"],
            "outfit": [],
        })

        repo.save_images.assert_called_once_with("task-1", [
            {"image_type": "mannequin", "storage_uri": "uploads/new.png"},
            {"image_type": "scene", "storage_uri": "uploads/scene.png"},
        ])
