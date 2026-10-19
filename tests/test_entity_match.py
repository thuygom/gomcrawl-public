from __future__ import annotations

import unittest

from core.entity_match import identity_label_for_result, score_entity_match, score_entity_match_batch


class EntityMatchTest(unittest.TestCase):
    def test_identity_label_uses_result_keyword_combinations(self) -> None:
        reference = {
            "person": {"이름": "김민수", "직책": "대표"},
            "company": {"회사명": "넥스트게이트"},
        }

        self.assertEqual(identity_label_for_result(reference, {"title": "김민수 넥스트게이트 인터뷰"}), "A")
        self.assertEqual(identity_label_for_result(reference, {"title": "김민수 대표 인터뷰"}), "B")
        self.assertEqual(identity_label_for_result(reference, {"title": "넥스트게이트 신규 서비스"}), "C")
        self.assertEqual(identity_label_for_result(reference, {"title": "김민수 인터뷰"}), "D")
        self.assertIsNone(identity_label_for_result(reference, {"title": "다른 회사 소식"}))
        self.assertEqual(
            identity_label_for_result(
                {"person": {"이름": "동명이"}, "company": {"회사명": "동명이"}},
                {"title": "동명이 인터뷰"},
            ),
            "D",
        )

    def test_structured_exact_email_match_is_strong(self) -> None:
        result = score_entity_match(
            {
                "person": {"이름": "Jane Kim", "이메일": "jane.kim@example.com"},
                "company": {"회사명": "Example AI", "홈페이지": "https://example.com"},
            },
            {
                "item_id": "r1",
                "title": "Jane Kim - Example AI",
                "snippet": "Contact Jane Kim at jane.kim@example.com",
                "url": "https://example.com/team/jane",
            },
        )

        self.assertGreaterEqual(result["score"], 0.95)
        self.assertEqual(result["label"], "strong_match")
        self.assertEqual(result["method"], "identity_match_formula_v2")
        self.assertIn("exact_person_email_match", result["signals"])
        self.assertNotIn("notes", result)

    def test_name_plus_company_context_is_likely_match(self) -> None:
        result = score_entity_match(
            {
                "person": {"이름": "김민수", "직책": "대표", "부서": None},
                "company": {"회사명": "넥스트게이트", "업종": "AI 솔루션", "홈페이지": "nextsgate.ai"},
            },
            {
                "item_id": "r1",
                "title": "넥스트게이트 김민수 대표 인터뷰",
                "snippet": "AI 솔루션 기업 넥스트게이트는 신규 서비스를 공개했다.",
                "url": "https://nextsgate.ai/news/interview",
            },
        )

        self.assertGreaterEqual(result["score"], 0.8)
        self.assertIn("person_name_phrase_match", result["signals"])
        self.assertIn("company_name_phrase_match", result["signals"])

    def test_company_number_can_anchor_company_but_person_missing_caps_score(self) -> None:
        result = score_entity_match(
            {
                "person": {"이름": "Jane Kim"},
                "company": {"회사명": "Example AI", "사업자번호": "123-45-67890"},
            },
            {
                "item_id": "r1",
                "title": "Example AI company profile",
                "snippet": "Business registration number 123-45-67890",
                "url": "https://example.ai/about",
            },
        )

        self.assertGreaterEqual(result["score"], 0.55)
        self.assertLessEqual(result["score"], 0.9)
        self.assertIn("exact_business_registration_number_match", result["signals"])
        self.assertIn("person_name_not_seen", result["risk_flags"])

    def test_unrelated_result_stays_low(self) -> None:
        result = score_entity_match(
            {
                "person": {"이름": "Jane Kim", "이메일": "jane.kim@example.com"},
                "company": {"회사명": "Example AI"},
            },
            {
                "item_id": "r1",
                "title": "Quarterly earnings report",
                "snippet": "Revenue and operating profit",
                "url": "https://finance.example.net/report",
            },
        )

        self.assertLessEqual(result["score"], 0.2)
        self.assertIn("no_direct_reference_overlap", result["risk_flags"])

    def test_batch_returns_summary_without_notes(self) -> None:
        summary, annotations = score_entity_match_batch(
            {"person": {"이름": "Jane Kim"}, "company": {"회사명": "Example AI"}},
            [{"item_id": "r1", "title": "Jane Kim Example AI"}, {"item_id": "r2", "title": "Other Person"}],
        )

        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["method"], "identity_match_formula_v2")
        self.assertEqual(summary["item_count"], 2)
        self.assertNotIn("notes", summary)
        self.assertIn("r1", annotations)
        self.assertIn("r2", annotations)

    def test_school_context_lifts_person_match(self) -> None:
        result = score_entity_match(
            {"person": {"이름": "김정훈", "school": "Hansung University"}},
            {
                "item_id": "r1",
                "title": "김정훈",
                "url": "https://www.linkedin.com/in/example",
                "raw_payload_json": {"matched_filters": {"schools": ["Hansung University"]}},
            },
        )

        self.assertGreaterEqual(result["score"], 0.86)
        self.assertIn("school_match", result["signals"])


if __name__ == "__main__":
    unittest.main()
