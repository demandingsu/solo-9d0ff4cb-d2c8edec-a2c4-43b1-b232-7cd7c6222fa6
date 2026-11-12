import unittest
from datetime import time

from tendercheck.models import KIND_CANCELLATION, KIND_CORRECTION, KIND_ORIGINAL
from tendercheck.parsing import (
    detect_kind,
    extract_deadline,
    extract_publish_time,
    normalize_project,
    parse_datetime,
)


class ParsingTests(unittest.TestCase):
    def test_parse_chinese_datetime(self):
        value = parse_datetime("发布日期：2026年10月8日 09:30")
        self.assertIsNotNone(value)
        self.assertEqual(value.strftime("%Y-%m-%d %H:%M"), "2026-10-08 09:30")
        self.assertEqual(value.utcoffset().total_seconds(), 8 * 3600)

    def test_extract_publish_time(self):
        value = extract_publish_time("公告类型：更正公告 发布时间：2026-10-20 14:30")
        self.assertEqual(value.strftime("%Y-%m-%d %H:%M"), "2026-10-20 14:30")

    def test_correction_uses_new_deadline(self):
        deadline, _ = extract_deadline(
            "原投标截止时间为2026年11月20日17:00，现延期至2026年11月27日17:00。"
        )
        self.assertEqual(deadline.strftime("%Y-%m-%d %H:%M"), "2026-11-27 17:00")

    def test_unchanged_deadline_is_not_invented(self):
        deadline, _ = extract_deadline("本次更正不涉及时间安排，投标截止时间不变。")
        self.assertIsNone(deadline)

    def test_date_only_deadline_defaults_to_end_of_day(self):
        value = parse_datetime("2026年11月20日", default_time=time(23, 59, 59))
        self.assertEqual(value.strftime("%H:%M:%S"), "23:59:59")

    def test_project_normalization(self):
        self.assertEqual(normalize_project("ｇｓｂ－2026－001"), "GSB-2026-001")

    def test_kind_precedence(self):
        self.assertEqual(detect_kind("某项目取消公告"), KIND_CANCELLATION)
        self.assertEqual(detect_kind("某项目更正公告"), KIND_CORRECTION)
        self.assertEqual(detect_kind("某项目招标公告"), KIND_ORIGINAL)
        self.assertEqual(detect_kind("某项目更正及取消公告"), KIND_CANCELLATION)


if __name__ == "__main__":
    unittest.main()
