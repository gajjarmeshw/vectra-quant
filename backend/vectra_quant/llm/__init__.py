"""LLM prompt construction.

Kept deliberately thin: this package builds strings and nothing else. No
network calls, no client objects, no configuration — the caller owns all of
that, so the prompts stay trivially testable.
"""
