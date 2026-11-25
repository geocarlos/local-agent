"""Shared output helper for tools: cap long text and say so, so the model knows it was cut."""


def truncate(text: str, limit: int) -> str:
    """Trim `text` to `limit` characters and append a marker that says how much was cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"
