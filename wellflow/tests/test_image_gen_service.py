"""Node4 生图模型链回归测试。"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from wellflow.app.llm.image_gen_service import get_image_models


class ImageModelChainTest(unittest.IsolatedAsyncioTestCase):
    async def test_appends_fallback_when_channel_has_one_model(self):
        options = [SimpleNamespace(value="qwen-image-3.0")]
        with patch(
            "wellflow.app.api.model_options.fetch_model_options",
            new=AsyncMock(return_value=options),
        ), patch(
            "wellflow.app.llm.image_gen_service.settings.node4_image_models_fallback",
            ["qwen-image-3.0", "gpt-image-2"],
        ):
            self.assertEqual(await get_image_models(), ["qwen-image-3.0", "gpt-image-2"])

    async def test_uses_fallback_when_channel_lookup_fails(self):
        with patch(
            "wellflow.app.api.model_options.fetch_model_options",
            new=AsyncMock(side_effect=RuntimeError("upstream unavailable")),
        ), patch(
            "wellflow.app.llm.image_gen_service.settings.node4_image_models_fallback",
            ["qwen-image-3.0", "gpt-image-2"],
        ):
            self.assertEqual(await get_image_models(), ["qwen-image-3.0", "gpt-image-2"])
