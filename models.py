from dataclasses import dataclass
from enum import Enum
from typing import Optional, Dict, Any, Union


class SliceType(str, Enum):
    """分片类型枚举"""
    PARENT = "父分片"
    CHILD = "子分片"


@dataclass
class DocumentPage:
    """文档单页模型"""
    page_index: int
    page_content: str

    @classmethod
    def from_input(cls, item: Union[Dict[str, Any], "DocumentPage"]) -> "DocumentPage":
        if isinstance(item, DocumentPage):
            return item
        if isinstance(item, dict):
            page_index = item.get("page_index")
            if page_index is None:
                raise ValueError("Page object must contain 'page_index'")
            return cls(page_index=int(page_index), page_content=str(item.get("page_content", "")))
        raise TypeError(f"Unsupported page input type: {type(item)}")


@dataclass
class ChunkSlice:
    """分片输出模型

    仅保留用户指定的 8 个核心切片字段：
    - slice_id: 分片ID
    - slice_text: 分片内容
    - slice_type: 分片类型（父分片 / 子分片）
    - parent_slice_id: 父分片ID（子分片有，父分片为 None）
    - prev_slice_id: 上一个分片ID
    - next_slice_id: 下一个分片ID
    - start_page: 开始页号
    - end_page: 结束页号
    """
    slice_id: str
    slice_text: str
    slice_type: str
    parent_slice_id: Optional[str]
    prev_slice_id: Optional[str]
    next_slice_id: Optional[str]
    start_page: int
    end_page: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "slice_id": self.slice_id,
            "slice_text": self.slice_text,
            "slice_type": self.slice_type,
            "parent_slice_id": self.parent_slice_id,
            "prev_slice_id": self.prev_slice_id,
            "next_slice_id": self.next_slice_id,
            "start_page": self.start_page,
            "end_page": self.end_page,
        }
