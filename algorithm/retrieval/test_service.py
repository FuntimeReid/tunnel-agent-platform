"""Small parameter/forwarding checks; no database or model requests."""
import unittest
from unittest.mock import patch

from pydantic import ValidationError

import library_catalog

with patch.object(library_catalog, "libraries", return_value=[]):
    import retrieval_service as service

LIBRARY = "a" * 32


class ServiceTests(unittest.TestCase):
    def test_input_boundaries(self):
        valid = {"library_id": LIBRARY, "text": "查找裂缝识别资料"}
        self.assertEqual(service.SearchInput(**valid).mode, "standard")
        for delta in [{"library_id": "bad"}, {"text": ""}, {"text": "字" * 8001},
                      {"mode": "ultra"}, {"top_n": 0}, {"top_n": 65},
                      {"top_n": True}, {"answer": True}, {"library_id": [LIBRARY]}]:
            with self.subTest(delta=list(delta)):
                with self.assertRaises(ValidationError):
                    service.SearchInput(**dict(valid, **delta))
        self.assertEqual(len(service.SearchInput(library_id=LIBRARY, text="字" * 8000).text), 8000)

    def test_blank_or_unknown_library_does_not_call_algorithm(self):
        with patch.object(service, "algorithm_call") as algorithm:
            result = service.search(service.SearchInput(library_id=LIBRARY, text="  "), None)
            self.assertEqual(result.status_code, 400)
            algorithm.assert_not_called()
        with patch.object(service, "registry_library", return_value=None), \
                patch.object(service, "algorithm_call") as algorithm:
            result = service.search(service.SearchInput(library_id=LIBRARY, text="问题"), None)
            self.assertEqual(result.status_code, 404)
            algorithm.assert_not_called()

    def test_forwarding_and_complete_pool(self):
        rows = [{"chunk_id": str(i), "document_id": "doc", "document_name": "资料.pdf",
                 "text": "完整原文"} for i in range(60)]
        body = {"code": 0, "request_id": "upstream-id", "data": {"sources": rows,
                "mode": "standard", "implementation_revision": "opt-r2-focused-documents"}}
        with patch.object(service, "registry_library", return_value={"id": LIBRARY}), \
                patch.object(service, "algorithm_call", return_value=(200, body)) as algorithm:
            result = service.search(service.SearchInput(library_id=LIBRARY, text=" 问题 "), "test-agent")
        payload = algorithm.call_args.args[1]
        self.assertEqual(payload["dataset_ids"], [LIBRARY])
        self.assertEqual(payload["question"], "问题")
        self.assertEqual(payload["output"], "chunks")
        self.assertEqual(payload["mode"], "standard")
        self.assertEqual(len(result["data"]["chunks"]), 60)
        self.assertNotIn("answer", result["data"])
        self.assertEqual(result["data"]["chunks"][0]["doc_id"], "doc")

    def test_upstream_failure_and_empty_result(self):
        with patch.object(service, "registry_library", return_value={"id": LIBRARY}):
            with patch.object(service, "algorithm_call", return_value=(401, {"code": "bad_key"})):
                result = service.search(service.SearchInput(library_id=LIBRARY, text="问题"), None)
                self.assertEqual(result.status_code, 502)
            with patch.object(service, "algorithm_call", side_effect=TimeoutError):
                result = service.search(service.SearchInput(library_id=LIBRARY, text="问题"), None)
                self.assertEqual(result.status_code, 504)
            with patch.object(service, "algorithm_call", return_value=(200, {"code": 0, "data": {
                "sources": [], "evidence_status": {"status": "INSUFFICIENT"},
                "missing_requested_sources": [{"reference": "P-999"}]}})):
                result = service.search(service.SearchInput(library_id=LIBRARY, text="问题"), None)
                self.assertEqual(result["data"]["chunks"], [])
                self.assertEqual(result["data"]["evidence_status"]["status"], "INSUFFICIENT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
