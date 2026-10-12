"""
单元测试用例：法律语义文档切片器 (Legal Semantic Document Chunker)
针对精简后的切片逻辑进行全方位单元测试：
1. P0 法律条款优先切分（第一条、第二条...完整性保护）
2. P1 自然段落降级（通知公告无条文时切分）
3. P2 超长文本兜底与重叠率（Overlap）
4. 特殊规则保护（《法规文件名》与 URL 不被腰斩）
5. 跨页精准映射（start_page 与 end_page 准确定位）
6. 父子分片与双向链表（parent_slice_id, prev_slice_id, next_slice_id）
7. 严格的出参字段规范（仅含 8 个核心字段）
"""

import unittest
from chunker import LegalDocChunker, paginate_and_chunk
from models import DocumentPage, SliceType


class TestLegalDocChunker(unittest.TestCase):

    def setUp(self):
        self.chunker = LegalDocChunker(
            max_chunk_size=200,
            min_chunk_size=30,
            overlap_ratio=0.15,
            parent_window_size=2
        )
        self.required_keys = {
            "slice_id",
            "slice_text",
            "slice_type",
            "parent_slice_id",
            "prev_slice_id",
            "next_slice_id",
            "start_page",
            "end_page"
        }

    def test_p0_article_split(self):
        """测试 P0：按法律条款切分，条文序号与正文完整保留"""
        pages = [
            {
                "page_index": 1,
                "page_content": (
                    "第一条 为了规范政府投资基金，制定本办法。\n"
                    "第二条 本办法所称政府投资基金，是指由政府出资设立的基金。\n"
                    "第三条 投资基金应当遵循市场化运作原则。"
                )
            }
        ]
        slices = self.chunker.chunk_pages(pages)
        child_slices = [s for s in slices if s["slice_type"] == SliceType.CHILD.value]

        self.assertEqual(len(child_slices), 3)
        self.assertTrue(child_slices[0]["slice_text"].startswith("第一条"))
        self.assertTrue(child_slices[1]["slice_text"].startswith("第二条"))
        self.assertTrue(child_slices[2]["slice_text"].startswith("第三条"))

        for s in slices:
            self.assertEqual(set(s.keys()), self.required_keys)

    def test_p1_paragraph_fallback(self):
        """测试 P1：无条文序号时，按自然段落（换行）切分"""
        pages = [
            {
                "page_index": 1,
                "page_content": (
                    "关于发布新规的通知\n\n"
                    "各相关单位：\n为进一步优化营商环境，现将有关事项通知如下。\n\n"
                    "请各单位高度重视，认真贯彻落实。"
                )
            }
        ]
        slices = paginate_and_chunk(pages)
        child_slices = [s for s in slices if s["slice_type"] == SliceType.CHILD.value]

        self.assertEqual(len(child_slices), 3)
        self.assertIn("通知", child_slices[0]["slice_text"])
        self.assertIn("各相关单位", child_slices[1]["slice_text"])
        self.assertIn("贯彻落实", child_slices[2]["slice_text"])

    def test_p2_length_split_and_overlap(self):
        """测试 P2：超长文本切分及重叠率"""
        long_sentence = "这是一段非常长的测试文本，用于检验切片器在超出最大限制时的递归标点切分效果。" * 10
        pages = [
            {
                "page_index": 1,
                "page_content": long_sentence
            }
        ]
        short_chunker = LegalDocChunker(max_chunk_size=100, overlap_ratio=0.15)
        slices = short_chunker.chunk_pages(pages)
        child_slices = [s for s in slices if s["slice_type"] == SliceType.CHILD.value]

        self.assertGreater(len(child_slices), 1)
        for s in child_slices:
            self.assertLessEqual(len(s["slice_text"]), 120)

    def test_protected_spans_law_title_and_url(self):
        """测试特殊规则：保护《法规名称》和 URL 不被切断"""
        text = "依据《中华人民共和国证券投资基金法实施细则全文规定》以及官方网址 https://www.gov.cn/zhengce/2023.html 执行。"
        pages = [{"page_index": 1, "page_content": text}]

        chunker = LegalDocChunker(max_chunk_size=50, overlap_ratio=0.1)
        slices = chunker.chunk_pages(pages)
        child_slices = [s for s in slices if s["slice_type"] == SliceType.CHILD.value]

        full_reconstructed = "".join(s["slice_text"] for s in child_slices)
        self.assertIn("《中华人民共和国证券投资基金法实施细则全文规定》", full_reconstructed)
        self.assertIn("https://www.gov.cn/zhengce/2023.html", full_reconstructed)

        # 检查每个切片中没有把《》拆半
        for s in child_slices:
            txt = s["slice_text"]
            self.assertEqual(txt.count("《"), txt.count("》"))

    def test_cross_page_tracking(self):
        """测试跨页追踪：单一条款跨页时，start_page 和 end_page 正确识别"""
        pages = [
            {
                "page_index": 1,
                "page_content": "第一条 前半部分在第一页。\n第二条 本条第一款在第一页。"
            },
            {
                "page_index": 2,
                "page_content": "第二款在第二页继续书写。\n第三条 全部在第二页。"
            }
        ]
        slices = paginate_and_chunk(pages)
        child_slices = [s for s in slices if s["slice_type"] == SliceType.CHILD.value]

        # 第一条 在第 1 页
        art1 = next(s for s in child_slices if "第一条" in s["slice_text"])
        self.assertEqual(art1["start_page"], 1)
        self.assertEqual(art1["end_page"], 1)

        # 第二条 跨第 1 页到第 2 页
        art2 = next(s for s in child_slices if "第二条" in s["slice_text"])
        self.assertEqual(art2["start_page"], 1)
        self.assertEqual(art2["end_page"], 2)

        # 第三条 在第 2 页
        art3 = next(s for s in child_slices if "第三条" in s["slice_text"])
        self.assertEqual(art3["start_page"], 2)
        self.assertEqual(art3["end_page"], 2)

    def test_parent_child_linking_and_bidirectional_chain(self):
        """测试父子分片关联及双向链表 prev/next 正确性"""
        pages = [
            {
                "page_index": 1,
                "page_content": "\n".join([f"第{i}条 正文内容测试第{i}条。" for i in range(1, 7)])
            }
        ]
        # parent_window_size=2 -> 6 个子块，对应 3 个父块
        chunker = LegalDocChunker(parent_window_size=2)
        slices = chunker.chunk_pages(pages)

        parents = [s for s in slices if s["slice_type"] == SliceType.PARENT.value]
        children = [s for s in slices if s["slice_type"] == SliceType.CHILD.value]

        # 检查子块双向链表
        for i, c in enumerate(children):
            self.assertIsNotNone(c["parent_slice_id"])
            if i == 0:
                self.assertIsNone(c["prev_slice_id"])
            else:
                self.assertEqual(c["prev_slice_id"], children[i - 1]["slice_id"])
            if i == len(children) - 1:
                self.assertIsNone(c["next_slice_id"])
            else:
                self.assertEqual(c["next_slice_id"], children[i + 1]["slice_id"])

        # 检查父块双向链表
        for j, p in enumerate(parents):
            self.assertIsNone(p["parent_slice_id"])
            if j == 0:
                self.assertIsNone(p["prev_slice_id"])
            else:
                self.assertEqual(p["prev_slice_id"], parents[j - 1]["slice_id"])
            if j == len(parents) - 1:
                self.assertIsNone(p["next_slice_id"])
            else:
                self.assertEqual(p["next_slice_id"], parents[j + 1]["slice_id"])

    def test_strict_eight_fields(self):
        """测试仅包含严格的 8 个核心出参字段，无多余元数据"""
        pages = [{"page_index": 1, "page_content": "第一条 测试严格八字段规范。"}]
        slices = paginate_and_chunk(pages)
        for s in slices:
            self.assertEqual(set(s.keys()), self.required_keys)
            self.assertNotIn("metadata", s)
            self.assertNotIn("slice_length", s)
            self.assertNotIn("head_text", s)
            self.assertNotIn("file_id", s)
            # 校验无-的UUID
            self.assertEqual(len(s["slice_id"]), 32)
            self.assertNotIn("-", s["slice_id"])
            int(s["slice_id"], 16)


if __name__ == "__main__":
    unittest.main()
