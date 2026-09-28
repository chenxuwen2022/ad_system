"""Prompt selections from intent classification keep their zero-based meaning."""
import unittest
from unittest.mock import patch

from wellflow.app.workflows.node4_graph import _prepare, resolve_prompt_indices


class PromptIndicesTests(unittest.TestCase):
    def test_numeric_strings_and_integers_select_the_same_prompts(self):
        for selection, expected in [
            (["1"], [1]), ([1], [1]), (["0", "2"], [0, 2]),
            (["2", 1, "2", "1"], [2, 1]),
            (None, [0, 1, 2]), ("all", [0, 1, 2]), ("last", [2]),
        ]:
            with self.subTest(selection=selection):
                self.assertEqual(resolve_prompt_indices(selection, 3), expected)

    def test_invalid_selection_is_rejected_without_widening_or_truncating(self):
        for selection in [
            [], "1", {}, [True], [False], [1.0], [1.5], [None],
            [-1], [3], ["-1"], ["3"], ["1.0"], [""], ["one"],
            ["0", "invalid"], ["0", "3"], [[1]],
        ]:
            with self.subTest(selection=selection):
                with self.assertRaisesRegex(ValueError, "提示词选择无效"):
                    resolve_prompt_indices(selection, 3)

    def test_missing_prompts_are_rejected(self):
        for selection in (None, "all", "last", ["0"]):
            with self.subTest(selection=selection):
                with self.assertRaisesRegex(ValueError, "缺少已确认的提示词"):
                    resolve_prompt_indices(selection, 0)


class PromptWorkItemsTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_prompt_string_index_only_queues_second_image(self):
        state = {
            "node1": {"compressed_images": ["data:image/png;base64,reference"]},
            "node3": {
                "generate_prompts": ["first prompt", "second prompt", "third prompt"],
                "per_prompt_size": ["1:1", "3:4", "4:3"],
                "image_model": "Doubao-Seedream-5.0-lite",
            },
            "node4": {"selected_prompt_indices": ["1"]},
        }
        with patch("wellflow.app.generation_journal.prepare_batch"), patch(
            "wellflow.app.workflows.node4_graph._ratio_to_size", return_value="1024x1792"
        ):
            result = await _prepare(state)
        items = result["node4"]["work_items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["prompt"], "second prompt")
        self.assertEqual(items[0]["prompt_index"], 1)
        self.assertEqual(items[0]["work_item_id"], "shot-02")
        self.assertEqual(items[0]["ratio"], "3:4")
        self.assertEqual(state["node4"]["selected_prompt_indices"], ["1"])


if __name__ == "__main__":
    unittest.main()
