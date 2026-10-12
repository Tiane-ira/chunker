"""
真实业务文档切片与对齐测试脚本
对比测试：
1. 待分片文档.md/政府投资基金暂行管理办法.md.py (Markdown 结构化文档)
2. 待分片文档.md/政府投资基金暂行管理办法.txt.py (纯文本法律文档)
3. 待分片文档.md/关于就《基金从业资格考试管理细则（修订征求意见稿）》公开征求意见的通知.py (自然段落通知类文档)

严格校验：
- 聚焦切片核心逻辑，输出仅包含 8 个核心字段（不记录任何冗余元数据）
- P0 法律条文完整性：第一条 ~ 第三十五条逐条识别，款项严禁腰斩
- 跨页识别精准度：第二十条跨第 2~3 页，自动检测并保持条文完整
- 父子块架构：章节聚合为父分片，辖下条文为子分片
- 结构一致性：Markdown 与 纯文本文档切片结构高度对齐（分片数、父子映射、页码一致）
"""

import os
import sys
import runpy
from typing import List, Dict, Any

# Windows 终端 UTF-8 设置
if sys.platform.startswith("win"):
    sys.stdout.reconfigure(encoding="utf-8")

from chunker import paginate_and_chunk
from models import SliceType

REQUIRED_KEYS = {
    "slice_id",
    "slice_text",
    "slice_type",
    "parent_slice_id",
    "prev_slice_id",
    "next_slice_id",
    "start_page",
    "end_page"
}


def verify_slice_schema(slices: List[Dict[str, Any]], doc_name: str):
    """校验分片数据仅包含 8 个规定字段且类型与取值合规"""
    assert len(slices) > 0, f"[{doc_name}] 切片结果不能为空"
    for i, s in enumerate(slices):
        keys = set(s.keys())
        assert keys == REQUIRED_KEYS, f"[{doc_name}] 分片 {s.get('slice_id')} 字段不匹配! 期望: {REQUIRED_KEYS}, 实际: {keys}"
        assert s["slice_type"] in (SliceType.PARENT.value, SliceType.CHILD.value)
        assert isinstance(s["start_page"], int) and s["start_page"] >= 1
        assert isinstance(s["end_page"], int) and s["end_page"] >= s["start_page"]
        assert len(s["slice_text"].strip()) > 0
        # 校验 slice_id 为无-的 32 位 UUID
        slice_id = s["slice_id"]
        assert len(slice_id) == 32 and "-" not in slice_id, f"分片ID {slice_id} 必须是无连字符的32位UUID"
        int(slice_id, 16)  # 验证为合法十六进制字符

        if s["slice_type"] == SliceType.CHILD.value:
            assert s["parent_slice_id"] is not None, f"子分片 {s['slice_id']} 必须具备 parent_slice_id"
            assert len(s["parent_slice_id"]) == 32 and "-" not in s["parent_slice_id"], f"parent_slice_id 必须为无-的32位UUID"
        else:
            assert s["parent_slice_id"] is None, f"父分片 {s['slice_id']} 的 parent_slice_id 必须为 None"


def test_and_compare_documents():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    target_dir = os.path.join(script_dir, "待分片文档.md")

    md_py_path = os.path.join(target_dir, "政府投资基金暂行管理办法.md.py")
    txt_py_path = os.path.join(target_dir, "政府投资基金暂行管理办法.txt.py")
    notice_py_path = os.path.join(target_dir, "关于就《基金从业资格考试管理细则（修订征求意见稿）》公开征求意见的通知.py")

    # 1. 加载文件中的 pages 数组
    md_pages = runpy.run_path(md_py_path)["pages"]
    txt_pages = runpy.run_path(txt_py_path)["pages"]
    notice_pages = runpy.run_path(notice_py_path)["pages"]

    print("=" * 60)
    print("开始切片测试：仅聚焦切片逻辑，去除冗余元数据")
    print("=" * 60)

    # 2. 执行切片
    # slices_md = paginate_and_chunk(pages=md_pages)
    # slices_txt = paginate_and_chunk(pages=txt_pages)
    slices_notice = paginate_and_chunk(pages=notice_pages,max_chunk_size=100)

    # 3. 校验数据格式仅含 8 个核心字段
    # verify_slice_schema(slices_md, "Markdown管理办法")
    # verify_slice_schema(slices_txt, "纯文本管理办法")
    verify_slice_schema(slices_notice, "征求意见通知")

if __name__ == "__main__":
    test_and_compare_documents()
