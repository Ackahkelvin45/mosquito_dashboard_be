from typing import Generic, List, Sequence, Tuple, TypeVar
from pydantic import BaseModel

T = TypeVar("T")

DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class Page(BaseModel, Generic[T]):
    """Standard paginated envelope returned by all list endpoints."""
    items: List[T]
    total: int
    page: int
    page_size: int
    total_pages: int


def paginate(items: Sequence, page: int, page_size: int) -> Tuple[list, int, int]:
    """Slice an in-memory sequence for the requested page.

    Returns (page_slice, total, total_pages).
    """
    total = len(items)
    total_pages = (total + page_size - 1) // page_size if page_size else 0
    start = (page - 1) * page_size
    return list(items[start:start + page_size]), total, total_pages
