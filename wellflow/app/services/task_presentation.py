"""Public task state excludes image bytes cached for workflow execution."""


def public_task_node(node, *, generation=False):
    if not isinstance(node, dict):
        return node
    internal = {"compressed_images", "compressed_model_images", "reference_images_data_uris"}
    if generation:
        internal.add("reference_images")
    return {key: value for key, value in node.items() if key not in internal}
