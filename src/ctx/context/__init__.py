"""Context retrieval and Markdown assembly."""

from .builder import build_markdown
from .search import SearchHit, search

__all__ = ["search", "SearchHit", "build_markdown"]
