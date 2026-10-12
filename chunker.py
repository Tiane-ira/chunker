"""
法律文档语义切片器 (Legal Semantic Document Chunker)

聚焦切片核心算法逻辑，严格遵循《需求.txt》设计规范：
1. P0 条款优先：完整识别“第X条”、“第X章”、“（一）”、“一、”等结构化序号（兼容 Markdown 与纯文本）；
2. P1 段落降级：针对通知、决议等无明确条文的文本按自然段落（换行）切分；
3. P2 长度兜底：超长文本按多级标点递归切分，单块不超过 max_chunk_size，相邻块支持 10%-15% 整句重叠；
4. 特殊规则保护：强制保护《》法规文件名及 URL 链接不被中间腰斩；
5. 跨页精准映射：字符偏移量快速二分定位每个分片的 start_page 和 end_page；
6. 父子分片关联：子分片用于精准向量比对，父分片聚合完整上下文供大模型读取，全链维护双向链表。
"""

import re
import bisect
import uuid
from typing import List, Dict, Any, Optional, Tuple, Union
from models import DocumentPage, ChunkSlice, SliceType


# ==============================================================================
# 第一部分：结构化正则定义 (正则表达式体系)
# ==============================================================================

# P0 法律条款/章节标题正则（行首匹配，兼容 Markdown 语法标头如 '### 第一条' 与纯文本 '第一条'）
# 规则说明：
# - `^[ \t]*`：允许行首有空格或缩进
# - `(?:#{1,6}\s*)?`：可选的 Markdown 标题前缀（# ~ ######）
# - `第[一...]+[编章节部卷篇]`：匹配编、章、节、部、卷、篇等层级标题
# - `第[一...]+条(?:\s*之[一...]+)?`：匹配法条编号，兼顾“第X条之一”等特殊附加条文
ARTICLE_HEADING_PATTERN = re.compile(
    r'(?m)^[ \t]*(?:#{1,6}\s*)?('
    r'第[一二三四五六七八九十百千\d]+[编章节部卷篇]|'
    r'第[一二三四五六七八九十百千\d]+条(?:\s*之[一二三四五六七八九十\d]+)?'
    r')'
)

# 层次 2：大项序号（适用于通知、批复、意见的第一级正文分类，如“一、指导思想”）
SECTION_HEADING_PATTERN = re.compile(
    r'(?m)^[ \t]*(?:#{1,6}\s*)?([一二三四五六七八九十]+[、\.])'
)

# 层次 3：子项序号（适用于款项内部清单项，如“（一）符合资质”、“1. 基本条件”）
ITEM_HEADING_PATTERN = re.compile(
    r'(?m)^[ \t]*(?:#{1,6}\s*)?([（\(][一二三四五六七八九十\d]+[）\)]|\d+[、\.](?!\d))'
)

# 层次 4：通用 Markdown 标题（针对未采用法条编号但具有标题层级的通用 Markdown 格式文档）
MARKDOWN_HEADING_PATTERN = re.compile(
    r'(?m)^[ \t]*(#{1,6}\s+[^\r\n]+)'
)

# 顶级章节标题正则（专门用于识别大章，用于驱动父分片的章节聚合分组）
CHAPTER_HEADING_PATTERN = re.compile(
    r'(?m)^[ \t]*(?:#{1,6}\s*)?('
    r'第[一二三四五六七八九十百千\d]+[编章节部卷篇]|'
    r'[一二三四五六七八九十]+[、\.]'
    r')'
)

# 严禁切断保护区：法律法规/文件名《...》及 URL 网址链接
# 避免在文本切片时将《中华人民共和国证券投资基金法》切碎，破坏精确检索词
PROTECTED_PATTERN = re.compile(r'《[^》\r\n]{1,300}》|https?://[^\s<>"]+')


# ==============================================================================
# 第二部分：页码映射与不可切断保护区追踪器
# ==============================================================================

class PageOffsetTracker:
    """字符偏移量与页码映射器
    
    【核心设计思路】：
    传统切片如果按“物理页”切分，会硬生生将跨页法条（如第二十条前半在P2，后半在P3）割裂为两段；
    本类将所有输入页面按顺序串联为一个无损的 `full_text`，并在底层记录每个页面在全文中的字符起止偏移量。
    在后续切片完成后，通过字符索引进行 O(log N) 的二分查找，精确反查出该切片对应的开始页码和结束页码。
    """
    def __init__(self, pages: List[DocumentPage]):
        # 严格按页码正序排序
        self.pages = sorted(pages, key=lambda p: p.page_index)
        self.full_text = ""
        # 记录每页的区间：[(char_start, char_end, page_index), ...]
        self.page_spans: List[Tuple[int, int, int]] = []

        chunks = []
        cur_offset = 0
        for idx, page in enumerate(self.pages):
            content = page.page_content or ""
            # 若前一页末尾与当前页开头均无换行符，则补充一个换行符作为页间自然过渡，避免粘字
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
        """根据切片在全文中的字符起止偏移量，精确定位其跨页范围 (start_page, end_page)
        
        例如第二十条起始于第 2 页末尾，结束于第 3 页开头，则返回 (2, 3)。
        """
        if not self.page_spans:
            return 1, 1
        start_pos = max(0, min(start_pos, len(self.full_text)))
        end_pos = max(start_pos, min(end_pos, len(self.full_text)))
        char_end = max(start_pos, end_pos - 1)
        return self._find_page(start_pos), self._find_page(char_end)

    def _find_page(self, pos: int) -> int:
        """通过二分搜索（bisect_right）在 O(log N) 时间内根据字符索引定位所在页码"""
        starts = [span[0] for span in self.page_spans]
        idx = max(0, min(bisect.bisect_right(starts, pos) - 1, len(self.page_spans) - 1))
        return self.page_spans[idx][2]


class ProtectedSpanTracker:
    """不可切断区间检测器
    
    【核心设计思路】：
    预先在全文中识别并提取所有《法规文件名》和 http(s) 网址的字符区间。
    当 P2 递归切片算法尝试在某处“下刀”时，若切点恰好落在某个书名号内部，
    算法将把切分点自动顺延至书名号闭合处（'》' 之后），彻底杜绝专有名词被截断。
    """
    def __init__(self, text: str):
        self.spans = [m.span() for m in PROTECTED_PATTERN.finditer(text)]
        self.spans.sort(key=lambda x: x[0])

    def get_enclosing_span(self, pos: int) -> Optional[Tuple[int, int]]:
        """检测指定字符位置是否位于某个保护区内部"""
        for s_start, s_end in self.spans:
            if s_start < pos < s_end:
                return (s_start, s_end)
            if s_start >= pos:
                break
        return None

    def adjust_split_point(self, pos: int) -> int:
        """若切分点落入保护区，则将切点外移至保护区末尾"""
        span = self.get_enclosing_span(pos)
        return span[1] if span else pos


# ==============================================================================
# 第三部分：切片中间数据单元
# ==============================================================================

class RawSegment:
    """初步切分得到的原始文本片段（中间结构）"""
    def __init__(self, text: str, start: int, end: int, chapter: str = ""):
        self.text = text            # 该片段的文本内容
        self.global_start = start   # 在全文中的起始字符索引
        self.global_end = end       # 在全文中的结束字符索引
        self.chapter = chapter      # 该片段所属的大章节标题（用于父块聚合）


# ==============================================================================
# 第四部分：文档切片核心引擎
# ==============================================================================

class LegalDocChunker:
    """法律语义文档切片器主类"""

    def __init__(
        self,
        max_chunk_size: int = 800,
        min_chunk_size: int = 50,
        overlap_ratio: float = 0.12,
        parent_window_size: int = 4,
        max_parent_size: int = 2400
    ):
        """初始化切片参数配置
        
        Args:
            max_chunk_size: 单个子分片的最大字符数上限（默认 800 字符，符合 LLM 检索的最佳上下文粒度）
            min_chunk_size: 判定章节标题是否为“空标题/无正文”的阈值（默认 50 字符）
            overlap_ratio: 超长文本切片时的相邻块重叠率（默认 0.12，即 12% 左右的上下文重叠）
            parent_window_size: 无章节结构的文档（如通知/公告）聚合父分片时的窗口子块数量（默认 4 个）
            max_parent_size: 单个父分片的最大字符数上限（默认 2400 字符），防止聚合上下文过长溢出大模型上下文
        """
        self.max_chunk_size = max_chunk_size
        self.min_chunk_size = min_chunk_size
        self.overlap_ratio = overlap_ratio
        self.parent_window_size = parent_window_size
        self.max_parent_size = max_parent_size

    def chunk_pages(
        self,
        pages: List[Union[Dict[str, Any], DocumentPage]]
    ) -> List[Dict[str, Any]]:
        """执行完整切片流水线（Pipeline）
        
        执行步骤：
        1. [页面接入与拼接]：解析 pages 数组，构建全文连续流与字符偏移量追踪器；
        2. [保护区标记]：预扫描全文《法规名称》和 URL，建立不可切断区间；
        3. [P0/P1 主切分]：按法条序号（第一条、第二条...）或自然段落提取语义块；
        4. [P2 长度兜底与重叠]：对超长块进行递归标点切分与滑动重叠（Overlap）；
        5. [页码映射]：二分反查每个子块的精确 (start_page, end_page)；
        6. [父子块聚合与双向链表]：按章节/窗口聚合父块，生成无连字符 UUID，串联前后双向链表；
        """
        doc_pages = [DocumentPage.from_input(p) for p in pages]
        if not doc_pages:
            return []

        # 1. 建立全文连续文本与偏移量映射
        tracker = PageOffsetTracker(doc_pages)
        full_text = tracker.full_text
        if not full_text.strip():
            return []

        # 2. 预标记《》书名号及 URL 保护区
        protected_tracker = ProtectedSpanTracker(full_text)

        # 3. 执行 P0 条款优先或 P1 自然段落切分
        raw_units = self._split_p0_p1(full_text)

        # 4. 执行 P2 长度兜底递归切分与重叠率控制
        child_segments = self._apply_p2_and_overlap(raw_units, protected_tracker)

        # 5. 构建原子子分片（赋独立 32 位 UUID 并映射起止页码）
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

        # 6. 构建父分片与双向链表关联
        all_slices = self._build_parent_child_hierarchy(
            child_slices,
            [s.chapter for s in child_segments if s.text.strip()]
        )
        return [s.to_dict() for s in all_slices]

    def _split_p0_p1(self, full_text: str) -> List[RawSegment]:
        """P0 / P1 主切分路由算法
        
        【优先级判断策略】：
        1. 优先扫描是否存在“第X条/第X章”等法律标准条款序号（P0）；
        2. 其次扫描大项序号（“一、”、“二、”）；
        3. 再次扫描清单序号（“（一）”、“1.”）；
        4. 再次扫描 Markdown 标题（“# 标题”）；
        5. 若均无结构化序号，则自动降级为双换行段落切分（P1）。
        """
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

            # 提取正文首条之前的前导文本（如法规名称、发文字号、发布日期等作为独立前言块）
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

                # 提取当前块的标题行
                line_end = full_text.find("\n", start)
                heading_line = full_text[start:line_end].strip() if line_end != -1 else full_text[start:].strip()
                is_chapter = bool(CHAPTER_HEADING_PATTERN.search(heading_line))

                if is_chapter:
                    current_chapter = heading_line
                    # 【空章节标题合并】
                    # 在许多文档中，“### 第一章 总则”后紧随“### 第一条”，章节自身并无实质正文（<= min_chunk_size）。
                    # 若将其单独切片会产生无意义的碎片短块；
                    # 因此，算法自动将纯章节标题与该章下的“第一条”合并为一个完整逻辑块，
                    # 既完整保留了章节层级上下文，又确保了 Markdown 与纯文本切片结果高度一致。
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
                            i += 2  # 一次性处理了章节标题和其首条
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
            # 无任何结构化序号，触发 P1 自然段落降级
            return self._split_by_paragraphs(full_text, 0)

    def _split_by_paragraphs(self, text: str, base_offset: int) -> List[RawSegment]:
        """P1 降级策略：以自然空行（连续换行符）切分段落
        
        适用于通知、批复、决议等非结构化流式长文档。
        """
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
        protected_tracker: ProtectedSpanTracker
    ) -> List[RawSegment]:
        """P2 长度兜底与相邻重叠控制
        
        【设计原则】：
        - 若法条/段落长度在 max_chunk_size（800 字符）以内：保持条文绝对纯洁性，整体保留，不与外部切片交叉重叠；
        - 若某条款特别冗长（> 800 字符）：触发多级标点递归切分，并在同一条款拆出的相邻子块间保留 10%~15% 的完整句子重叠。
        """
        overlap_size = int(self.max_chunk_size * self.overlap_ratio)
        final_segments: List[RawSegment] = []

        for unit in raw_units:
            if len(unit.text) <= self.max_chunk_size:
                final_segments.append(unit)
            else:
                # 超长段落递归切分并计算滑动重叠
                sub_segs = self._recursive_split(
                    unit.text,
                    unit.global_start,
                    overlap_size,
                    protected_tracker,
                    unit.chapter
                )
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
        """超长文本递归标点切分与滑动窗口重叠算法
        
        执行步骤：
        1. [句级切分]：利用句号、感叹号、问号、分号、换行符将文本切成完整的句子；
        2. [单句防爆]：若偶发极长单句依然超过 max_chunk_size，利用 protected_tracker 避开书名号后强制切断；
        3. [滑动合并与重叠回退]：逐句累加拼装切片。当累积将超过上限时封包当前切片，
           并从当前切片末尾逆向选取不超过 overlap_size 的完整句子，作为下一个切片的起始上下文。
        """
        # 步骤 1：按照自然标点断句
        regex = re.compile(r'([^。！？!？；;\n]+(?:[。！？!？；;\n]+|$))')
        raw_pieces = []
        for m in regex.finditer(text):
            p_text = m.group(0)
            if p_text:
                raw_pieces.append((p_text, base_offset + m.start(), base_offset + m.end()))

        # 步骤 2：对极罕见的超长单句做次级字符切分，并避让《》保护区
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
                        # 若切断点落在《法规名》中间，顺延至法规名末尾
                        global_cut = protected_tracker.adjust_split_point(global_cut)
                        cand_end = max(idx + 1, global_cut - p_start)
                    sub_str = p_text[idx:cand_end]
                    final_pieces.append((sub_str, p_start + idx, p_start + cand_end))
                    idx = cand_end

        # 步骤 3：滑动窗口合并并回退重叠整句
        merged: List[RawSegment] = []
        cur_pieces: List[Tuple[str, int, int]] = []
        cur_len = 0

        for piece_text, p_start, p_end in final_pieces:
            if cur_pieces and (cur_len + len(piece_text) > self.max_chunk_size):
                # 封包当前累积的分片
                full_chunk_text = "".join(p[0] for p in cur_pieces)
                merged.append(RawSegment(full_chunk_text, cur_pieces[0][1], cur_pieces[-1][2], chapter))

                # 从当前分片尾部逆向寻找完整句子，作为下一切片的重叠前缀
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

        # 封包最后一个分片
        if cur_pieces:
            full_chunk_text = "".join(p[0] for p in cur_pieces)
            merged.append(RawSegment(full_chunk_text, cur_pieces[0][1], cur_pieces[-1][2], chapter))

        return merged

    def _build_parent_child_hierarchy(
        self,
        child_slices: List[ChunkSlice],
        chapters: List[str]
    ) -> List[ChunkSlice]:
        """构建父子分片层级架构与同层双向链表
        
        【层次结构设计】：
        1. 串联子分片双向链表：所有子分片顺序连接（prev_slice_id / next_slice_id）；
        2. 聚合生成父分片：
           - 优先依据“章”边界聚合；
           - 当单章过长（超过 max_parent_size）或无章节时，按 parent_window_size 滑动窗口封包；
        3. 回填父子绑定：将父分片的 UUID 赋给旗下所有子分片的 parent_slice_id，父分片的页码范围取辖下子分片的极值；
        4. 串联父分片双向链表：父分片之间也互相记录 prev_slice_id 和 next_slice_id；
        5. 层级交替输出：按“父分片 1 -> 辖下子分片集合 -> 父分片 2 ...”顺序返回。
        """
        # 1. 串联子分片双向链表
        for i, c in enumerate(child_slices):
            c.prev_slice_id = child_slices[i - 1].slice_id if i > 0 else None
            c.next_slice_id = child_slices[i + 1].slice_id if i < len(child_slices) - 1 else None

        # 2. 章节分组或滑动窗口分组
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

        # 3. 实例化父分片并回填 parent_slice_id
        parent_slices: List[ChunkSlice] = []
        for p_idx, group in enumerate(groups):
            p_id = uuid.uuid4().hex  # 生成无连字符 32 位 UUID
            parent_text = "\n\n".join(c.slice_text for c in group)
            start_p = min(c.start_page for c in group)
            end_p = max(c.end_page for c in group)

            parent_slices.append(ChunkSlice(
                slice_id=p_id,
                slice_text=parent_text,
                slice_type=SliceType.PARENT.value,
                parent_slice_id=None,  # 父分片自身无父级
                prev_slice_id=None,
                next_slice_id=None,
                start_page=start_p,
                end_page=end_p
            ))
            # 辖下子分片绑定归属父分片
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
# 第五部分：统一快捷调用入口
# ==============================================================================

def paginate_and_chunk(
    pages: List[Union[Dict[str, Any], DocumentPage]],
    max_chunk_size: int = 800,
    parent_window_size: int = 4,
    overlap_ratio: float = 0.12
) -> List[Dict[str, Any]]:
    """分页切片器统一快捷入口函数

    入参:
        pages: 页面数组 [{"page_index": 1, "page_content": "..."}, ...]
        max_chunk_size: 子块最大字符数上限 (默认 800 字符)
        parent_window_size: 父块聚合窗口大小 (默认 4 个子块)
        overlap_ratio: 超长文本拆分时的重叠率 (默认 0.12，即 12% 整句重叠)

    出参:
        返回严格符合 8 字段规范的切片字典列表：
        - slice_id: 分片ID (无连字符的 32 位 UUID)
        - slice_text: 分片内容原文
        - slice_type: 分片类型 ("父分片" 或 "子分片")
        - parent_slice_id: 所属父分片ID (子分片为对应父分片ID，父分片自身为 None)
        - prev_slice_id: 上一个同层分片ID
        - next_slice_id: 下一个同层分片ID
        - start_page: 开始页号 (基于 1 的页码)
        - end_page: 结束页号 (跨页时体现)
    """
    chunker = LegalDocChunker(
        max_chunk_size=max_chunk_size,
        parent_window_size=parent_window_size,
        overlap_ratio=overlap_ratio
    )
    return chunker.chunk_pages(pages=pages)
