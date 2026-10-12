# 法律语义感知文档切片器 (Legal Semantic Document Chunker)

基于《需求.txt》规范设计的高性能文档分页与切片器，精简聚焦于**文档切片核心逻辑**，去除无关元数据记录。

---

## 一、核心特性与切片策略

1. **P0 条款优先（法律语义完整性）**：
   - 自动识别“第X章”、“第X条”、“一、”、“（一）”等层级结构化序号（兼容 Markdown 标题与纯文本）。
   - 以法律条款为最小不可分割逻辑单元，确保条文编号与全部款项正文完整保留，严禁腰斩。
2. **P1 自然段落降级**：
   - 针对无明确法条编号的通知、决议类文件，自动降级为按自然段落（换行符）切分。
3. **P2 长度兜底与递归切片**：
   - 针对超长条款/段落（默认超过 800 字符），采用标点多级递归字符切片（换行 -> 句号/问号/感叹号 -> 分号 -> 逗号）。
4. **特殊规则保护（严禁切断）**：
   - 自动保护《》书名号/法规名称以及 URL 网址链接，强制整体保留在同一分片中，防止法规名称被截断。
5. **切片语义重叠（Overlap）**：
   - 发生超长切分时，相邻子块自动保留 10%~15% 的重叠上下文，基于句意边界裁剪。
6. **父子块索引体系（Parent-Child Structure）**：
   - **子分片（Child Slice）**：最小原子块，用于检索比对（入向量库）。
   - **父分片（Parent Slice）**：子分片所在章节或上下文窗口聚合（供大模型读取）。
7. **跨页精准映射（Cross-Page Tracker）**：
   - 字符级偏移量追踪技术，精确计算每个分片的 `start_page` 与 `end_page`（如单条条文跨多页时自动精确标记）。
8. **双向链表关联（Prev / Next Slice ID）**：
   - 子块与父块各自维护 `prev_slice_id` 与 `next_slice_id`，支持无缝前后文漫游。

---

## 二、数据接口规格

### 1. 入参定义 (Input)
```python
pages = [
    {
        "page_index": 1,        # 当前页码 (int)
        "page_content": "..."   # 当前页文本内容 (str)
    },
    ...
]
```

### 2. 出参数据结构 (Output)
严格仅包含 8 个核心切片字段（不记录任何冗余元数据）：

| 字段名称 | 数据类型 | 说明 |
| `slice_id` | `str` | 分片ID（无连字符的32位UUID，如 `33c5a7de226b4d7fa2c555e7705e8374`） |
| `slice_text` | `str` | 分片内容 |
| `slice_type` | `str` | 分片类型：`"父分片"` 或 `"子分片"` |
| `parent_slice_id` | `Optional[str]` | 父分片ID（子分片类型才有，父分片为 `None`） |
| `prev_slice_id` | `Optional[str]` | 上一个分片ID（首个为 `None`） |
| `next_slice_id` | `Optional[str]` | 下一个分片ID（末尾为 `None`） |
| `start_page` | `int` | 开始页号 |
| `end_page` | `int` | 结束页号 |

---

## 三、快速开始

### 1. 基础调用

```python
from chunker import paginate_and_chunk

pages = [
    {
        "page_index": 1,
        "page_content": "第一条 为了规范政府投资基金，制定本办法。\n第二条 本办法所称政府投资基金..."
    },
    {
        "page_index": 2,
        "page_content": "是指由政府出资设立的基金。\n第三条 投资基金应当遵循市场化运作原则。"
    }
]

# 进行分页切片
slices = paginate_and_chunk(
    pages=pages,
    max_chunk_size=800,        # 单子块最大字符数
    parent_window_size=4,      # 父块包含子块数
    overlap_ratio=0.12         # 重叠率 12%
)

for s in slices:
    print(s)
```

### 2. 运行测试

```bash
# 运行核心切片单元测试
python -m unittest test_chunker.py -v

# 运行真实业务文档对比与跨页对齐测试
python test_actual_documents.py
```
