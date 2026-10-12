"""
法律文档语义切片器 (Legal Semantic Document Chunker)

聚焦切片核心逻辑，严格遵循《需求.txt》：
1. P0 条款优先：完整识别“第X条”、“第X章”、“（一）”、“一、”等结构化序号（兼容 Markdown 与纯文本）；
2. P1 段落降级：通知、决议等无明确条文的文本按自然段落切分；
3. P2 长度兜底：超长文本按多级标点递归切分，不超过 max_chunk_size，相邻块支持 10%-15% 重叠；
4. 特殊规则保护：强制保护《》法规文件名及 URL 链接不被腰斩；
5. 跨页精准映射：字符偏移量快速二分定位每个分片的 start_page 和 end_page；
6. 父子分片关联：子分片用于向量比对，父分片聚合上下文供大模型读取，全链双向链表关联。
"""

import re
import bisect
import uuid
from typing import List, Dict, Any, Optional, Tuple, Union
from models import DocumentPage, ChunkSlice, SliceType


# ==============================================================================
# 结构化正则定义
# ==============================================================================

# P0 法律结构化标题正则（行首匹配，兼容 Markdown 标头与纯文本）
ARTICLE_HEADING_PATTERN = re.compile(
    r'(?m)^[ \t]*(?:#{1,6}\s*)?('
    r'第[一二三四五六七八九十百千\d]+[编章节部卷篇]|'
    r'第[一二三四五六七八九十百千\d]+条(?:\s*之[一二三四五六七八九十\d]+)?'
    r')'
)

# 层次 2：大项序号（通知、意见第一级）
SECTION_HEADING_PATTERN = re.compile(
    r'(?m)^[ \t]*(?:#{1,6}\s*)?([一二三四五六七八九十]+[、\.])'
)

# 层次 3：子项序号
ITEM_HEADING_PATTERN = re.compile(
    r'(?m)^[ \t]*(?:#{1,6}\s*)?([（\(][一二三四五六七八九十\d]+[）\)]|\d+[、\.](?!\d))'
)

# 层次 4：通用 Markdown 标题（针对无条文序号的通用 Markdown 文档）
MARKDOWN_HEADING_PATTERN = re.compile(
    r'(?m)^[ \t]*(#{1,6}\s+[^\r\n]+)'
)

# 顶级章节标题正则（用于判断大父块聚合）
CHAPTER_HEADING_PATTERN = re.compile(
    r'(?m)^[ \t]*(?:#{1,6}\s*)?('
    r'第[一二三四五六七八九十百千\d]+[编章节部卷篇]|'
    r'[一二三四五六七八九十]+[、\.]'
    r')'
)

# 严禁切断保护区：《法律法规/文件名》与 URL 链接
PROTECTED_PATTERN = re.compile(r'《[^》\r\n]{1,300}》|https?://[^\s<>"]+')


# ==============================================================================
# 页码映射与不可切断保护区追踪
# ==============================================================================

class PageOffsetTracker:
    """字符偏移量与页码映射器"""
    def __init__(self, pages: List[DocumentPage]):
        self.pages = sorted(pages, key=lambda p: p.page_index)
        self.full_text = ""
        self.page_spans: List[Tuple[int, int, int]] = []  # (start, end, page_index)

        chunks = []
        cur_offset = 0
        for idx, page in enumerate(self.pages):
            content = page.page_content or ""
            delimiter = "\n" if (idx > 0 and chunks and not chunks[-1].endswith("\n") and not content.startswith("\n")) else ""
            if delimiter:
                chunks.append(delimiter)
                cur_offset += len(delimiter)
            start = cur_offset
            chunks.append(content)
            cur_offset += len(content)
            self.page_spans.append((start, cur_offset, page.page_index))

        self.full_text = "".join(chunks)

    def get_page_range(self, start_pos: int, end_pos: int) -> Tuple[int, int]:
        """根据字符起止索引精确定位 (start_page, end_page)"""
        if not self.page_spans:
            return 1, 1
        start_pos = max(0, min(start_pos, len(self.full_text)))
        end_pos = max(start_pos, min(end_pos, len(self.full_text)))
        char_end = max(start_pos, end_pos - 1)
        return self._find_page(start_pos), self._find_page(char_end)

    def _find_page(self, pos: int) -> int:
        starts = [span[0] for span in self.page_spans]
        idx = max(0, min(bisect.bisect_right(starts, pos) - 1, len(self.page_spans) - 1))
        return self.page_spans[idx][2]


class ProtectedSpanTracker:
    """不可切断区间检测器"""
    def __init__(self, text: str):
        self.spans = [m.span() for m in PROTECTED_PATTERN.finditer(text)]
        self.spans.sort(key=lambda x: x[0])

    def get_enclosing_span(self, pos: int) -> Optional[Tuple[int, int]]:
        for s_start, s_end in self.spans:
            if s_start < pos < s_end:
                return (s_start, s_end)
            if s_start >= pos:
                break
        return None

    def adjust_split_point(self, pos: int) -> int:
        span = self.get_enclosing_span(pos)
        return span[1] if span else pos


# ==============================================================================
# 切片中间单元
# ==============================================================================

class RawSegment:
    def __init__(self, text: str, start: int, end: int, chapter: str = ""):
        self.text = text
        self.global_start = start
        self.global_end = end
        self.chapter = chapter


# ==============================================================================
# 文档切片主类
# ==============================================================================

class LegalDocChunker:
    """文档切片器主类"""

    def __init__(
        self,
        max_chunk_size: int = 800,
        min_chunk_size: int = 50,
        overlap_ratio: float = 0.12,
        parent_window_size: int = 4,
        max_parent_size: int = 2400
    ):
        self.max_chunk_size = max_chunk_size
        self.min_chunk_size = min_chunk_size
        self.overlap_ratio = overlap_ratio
        self.parent_window_size = parent_window_size
        self.max_parent_size = max_parent_size

    def chunk_pages(
        self,
        pages: List[Union[Dict[str, Any], DocumentPage]]
    ) -> List[Dict[str, Any]]:
        """执行切片逻辑，仅输出切片核心出参"""
        doc_pages = [DocumentPage.from_input(p) for p in pages]
        if not doc_pages:
            return []

        tracker = PageOffsetTracker(doc_pages)
        full_text = tracker.full_text
        if not full_text.strip():
            return []

        protected_tracker = ProtectedSpanTracker(full_text)

        # 1. P0/P1 结构与段落切分
        raw_units = self._split_p0_p1(full_text)

        # 2. P2 长度兜底递归切分与重叠处理
        child_segments = self._apply_p2_and_overlap(raw_units, full_text, protected_tracker)

        # 3. 构建子分片
        child_slices: List[ChunkSlice] = []
        for idx, seg in enumerate(child_segments):
            clean_text = seg.text.strip()
            if not clean_text:
                continue
            start_page, end_page = tracker.get_page_range(seg.global_start, seg.global_end)
            child_slices.append(ChunkSlice(
                slice_id=uuid.uuid4().hex,
                slice_text=clean_text,
                slice_type=SliceType.CHILD.value,
                parent_slice_id=None,
                prev_slice_id=None,
                next_slice_id=None,
                start_page=start_page,
                end_page=end_page
            ))

        if not child_slices:
            return []

        # 4. 构建双向链表与父分片聚合
        all_slices = self._build_parent_child_hierarchy(child_slices, [s.chapter for s in child_segments if s.text.strip()])
        return [s.to_dict() for s in all_slices]

    def _split_p0_p1(self, full_text: str) -> List[RawSegment]:
        """P0/P1 主切分逻辑"""
        if ARTICLE_HEADING_PATTERN.search(full_text):
            active_pattern = ARTICLE_HEADING_PATTERN
        elif SECTION_HEADING_PATTERN.search(full_text):
            active_pattern = SECTION_HEADING_PATTERN
        elif ITEM_HEADING_PATTERN.search(full_text):
            active_pattern = ITEM_HEADING_PATTERN
        elif MARKDOWN_HEADING_PATTERN.search(full_text):
            active_pattern = MARKDOWN_HEADING_PATTERN
        else:
            active_pattern = None

        if active_pattern:
            matches = list(active_pattern.finditer(full_text))
            units: List[RawSegment] = []

            # 提取前言/标题部分
            if matches[0].start() > 0:
                preamble = full_text[:matches[0].start()]
                if preamble.strip():
                    units.extend(self._split_by_paragraphs(preamble, 0))

            current_chapter = ""
            i = 0
            while i < len(matches):
                m = matches[i]
                start = m.start()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
                sec_text = full_text[start:end]

                line_end = full_text.find("\n", start)
                heading_line = full_text[start:line_end].strip() if line_end != -1 else full_text[start:].strip()
                is_chapter = bool(CHAPTER_HEADING_PATTERN.search(heading_line))

                if is_chapter:
                    current_chapter = heading_line
                    # 章节标题无正文时，与所属首条条文自然合并
                    if len(sec_text.strip()) <= self.min_chunk_size and i + 1 < len(matches):
                        next_m = matches[i + 1]
                        next_end = matches[i + 2].start() if i + 2 < len(matches) else len(full_text)
                        next_line_end = full_text.find("\n", next_m.start())
                        next_heading = full_text[next_m.start():next_line_end].strip() if next_line_end != -1 else full_text[next_m.start():].strip()

                        if not CHAPTER_HEADING_PATTERN.search(next_heading):
                            units.append(RawSegment(
                                text=full_text[start:next_end],
                                start=start,
                                end=next_end,
                                chapter=current_chapter
                            ))
                            i += 2
                            continue

                units.append(RawSegment(
                    text=sec_text,
                    start=start,
                    end=end,
                    chapter=current_chapter
                ))
                i += 1
            return units
        else:
            return self._split_by_paragraphs(full_text, 0)

    def _split_by_paragraphs(self, text: str, base_offset: int) -> List[RawSegment]:
        """P1 自然空行切分段落"""
        units: List[RawSegment] = []
        p_pattern = re.compile(r'\n\s*\n')
        cur = 0
        for match in p_pattern.finditer(text):
            p_end = match.start()
            seg_text = text[cur:p_end]
            if seg_text.strip():
                units.append(RawSegment(seg_text, base_offset + cur, base_offset + p_end))
            cur = match.end()

        if cur < len(text):
            rem = text[cur:]
            if rem.strip():
                units.append(RawSegment(rem, base_offset + cur, base_offset + len(text)))

        if not units and text.strip():
            units.append(RawSegment(text, base_offset, base_offset + len(text)))
        return units

    def _apply_p2_and_overlap(
        self,
        raw_units: List[RawSegment],
        full_text: str,
        protected_tracker: ProtectedSpanTracker
    ) -> List[RawSegment]:
        """P2 长度兜底递归切分与重叠"""
        overlap_size = int(self.max_chunk_size * self.overlap_ratio)
        final_segments: List[RawSegment] = []

        for unit in raw_units:
            if len(unit.text) <= self.max_chunk_size:
                final_segments.append(unit)
            else:
                sub_segs = self._recursive_split(unit.text, unit.global_start, overlap_size, protected_tracker, unit.chapter)
                final_segments.extend(sub_segs)
        return final_segments

    def _recursive_split(
        self,
        text: str,
        base_offset: int,
        overlap_size: int,
        protected_tracker: ProtectedSpanTracker,
        chapter: str
    ) -> List[RawSegment]:
        """递归切分并避让保护区"""
        # 句级切分
        regex = re.compile(r'([^。！？!？；;\n]+(?:[。！？!？；;\n]+|$))')
        raw_pieces = []
        for m in regex.finditer(text):
            p_text = m.group(0)
            if p_text:
                raw_pieces.append((p_text, base_offset + m.start(), base_offset + m.end()))

        # 超长单句细分
        final_pieces: List[Tuple[str, int, int]] = []
        for p_text, p_start, p_end in raw_pieces:
            if len(p_text) <= self.max_chunk_size:
                final_pieces.append((p_text, p_start, p_end))
            else:
                idx = 0
                while idx < len(p_text):
                    step = min(self.max_chunk_size, len(p_text) - idx)
                    cand_end = idx + step
                    global_cut = p_start + cand_end
                    if cand_end < len(p_text):
                        global_cut = protected_tracker.adjust_split_point(global_cut)
                        cand_end = max(idx + 1, global_cut - p_start)
                    sub_str = p_text[idx:cand_end]
                    final_pieces.append((sub_str, p_start + idx, p_start + cand_end))
                    idx = cand_end

        # 合并并应用重叠
        merged: List[RawSegment] = []
        cur_pieces: List[Tuple[str, int, int]] = []
        cur_len = 0

        for piece_text, p_start, p_end in final_pieces:
            if cur_pieces and (cur_len + len(piece_text) > self.max_chunk_size):
                full_chunk_text = "".join(p[0] for p in cur_pieces)
                merged.append(RawSegment(full_chunk_text, cur_pieces[0][1], cur_pieces[-1][2], chapter))

                overlap_pieces: List[Tuple[str, int, int]] = []
                acc = 0
                for rev in reversed(cur_pieces):
                    if acc + len(rev[0]) <= overlap_size:
                        overlap_pieces.insert(0, rev)
                        acc += len(rev[0])
                    else:
                        break
                cur_pieces = list(overlap_pieces)
                cur_len = sum(len(p[0]) for p in cur_pieces)

            cur_pieces.append((piece_text, p_start, p_end))
            cur_len += len(piece_text)

        if cur_pieces:
            full_chunk_text = "".join(p[0] for p in cur_pieces)
            merged.append(RawSegment(full_chunk_text, cur_pieces[0][1], cur_pieces[-1][2], chapter))

        return merged

    def _build_parent_child_hierarchy(
        self,
        child_slices: List[ChunkSlice],
        chapters: List[str]
    ) -> List[ChunkSlice]:
        """构建父子分片结构与双向链表"""
        # 1. 串联子分片双向链表
        for i, c in enumerate(child_slices):
            c.prev_slice_id = child_slices[i - 1].slice_id if i > 0 else None
            c.next_slice_id = child_slices[i + 1].slice_id if i < len(child_slices) - 1 else None

        # 2. 章节或窗口分组
        groups: List[List[ChunkSlice]] = []
        cur_group: List[ChunkSlice] = []
        cur_len = 0
        cur_chapter: Optional[str] = None

        for c, ch in zip(child_slices, chapters):
            chapter_changed = (cur_chapter is not None and ch != cur_chapter)
            if cur_group and (
                chapter_changed or
                cur_len + len(c.slice_text) > self.max_parent_size or
                (not ch and len(cur_group) >= self.parent_window_size)
            ):
                groups.append(cur_group)
                cur_group = [c]
                cur_len = len(c.slice_text)
                cur_chapter = ch
            else:
                cur_group.append(c)
                cur_len += len(c.slice_text)
                if cur_chapter is None:
                    cur_chapter = ch

        if cur_group:
            groups.append(cur_group)

        # 3. 生成父分片并回填关联
        parent_slices: List[ChunkSlice] = []
        for p_idx, group in enumerate(groups):
            p_id = uuid.uuid4().hex
            parent_text = "\n\n".join(c.slice_text for c in group)
            start_p = min(c.start_page for c in group)
            end_p = max(c.end_page for c in group)

            parent_slices.append(ChunkSlice(
                slice_id=p_id,
                slice_text=parent_text,
                slice_type=SliceType.PARENT.value,
                parent_slice_id=None,
                prev_slice_id=None,
                next_slice_id=None,
                start_page=start_p,
                end_page=end_p
            ))
            for c in group:
                c.parent_slice_id = p_id

        # 4. 串联父分片双向链表
        for j, p in enumerate(parent_slices):
            p.prev_slice_id = parent_slices[j - 1].slice_id if j > 0 else None
            p.next_slice_id = parent_slices[j + 1].slice_id if j < len(parent_slices) - 1 else None

        # 5. 层级交替输出 (父分片 -> 辖下子分片)
        all_slices: List[ChunkSlice] = []
        for p, group in zip(parent_slices, groups):
            all_slices.append(p)
            all_slices.extend(group)
        return all_slices


# ==============================================================================
# 统一入口函数
# ==============================================================================

def paginate_and_chunk(
    pages: List[Union[Dict[str, Any], DocumentPage]],
    max_chunk_size: int = 800,
    parent_window_size: int = 4,
    overlap_ratio: float = 0.12
) -> List[Dict[str, Any]]:
    """分页切片器统一快捷入口

    入参:
        pages: 页面数组 [{"page_index": 1, "page_content": "..."}, ...]
        max_chunk_size: 子块最大字符数 (默认 800)
        parent_window_size: 父块聚合窗口大小 (默认 4)
        overlap_ratio: 重叠率 (默认 0.12)

    出参:
        仅包含规范中要求的核心字段列表：
        - slice_id: 分片ID (无连字符的32位UUID)
        - slice_text: 分片内容
        - slice_type: 分片类型 ("父分片" 或 "子分片")
        - parent_slice_id: 父分片ID (子分片为对应父分片ID，父分片为None)
        - prev_slice_id: 上一个分片ID
        - next_slice_id: 下一个分片ID
        - start_page: 开始页号
        - end_page: 结束页号
    """
    chunker = LegalDocChunker(
        max_chunk_size=max_chunk_size,
        parent_window_size=parent_window_size,
        overlap_ratio=overlap_ratio
    )
    return chunker.chunk_pages(pages=pages)
