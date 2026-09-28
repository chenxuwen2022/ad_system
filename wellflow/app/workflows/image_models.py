"""Validate an explicitly selected model without substituting a different model."""


def selected_image_model(value):
    if value is None:
        return None
    # The selector is backed by the dynamic model catalog. Model availability
    # is resolved by the image client; do not maintain a second static allowlist.
    if not isinstance(value, str) or not value.strip():
        raise ValueError("请选择生图模型")
    return value.strip()
