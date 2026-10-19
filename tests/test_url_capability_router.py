import unittest
import os
from core.url_capability_router.classifier import Classifier
from core.url_capability_router.models import HandlingMode, ClassificationOptions
from unittest.mock import patch, MagicMock

class TestUrlCapabilityRouter(unittest.TestCase):
    def setUp(self):
        # We assume core/url_capability_router/platform-rules.yaml exists relative to the project root
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        yaml_path = os.path.join(project_root, "core", "url_capability_router", "platform-rules.yaml")
        self.classifier = Classifier(yaml_path=yaml_path)
        self.options = ClassificationOptions(probe=False, allow_private_network=False)

    def test_youtube(self):
        res = self.classifier.classify("https://www.youtube.com/watch?v=abc", self.options)
        self.assertEqual(res.classification, "youtube")
        self.assertEqual(res.handling_mode, HandlingMode.PLATFORM_EXTRACTOR)
        self.assertTrue(res.crawlable)
        self.assertEqual(res.preferred_handler, "youtube_transcript")
        
    def test_google_docs(self):
        res = self.classifier.classify("https://docs.google.com/document/d/1234/edit", self.options)
        self.assertEqual(res.classification, "google_drive")
        self.assertEqual(res.resource_type, "document")
        self.assertEqual(res.handling_mode, HandlingMode.API_CONNECTOR)
        self.assertFalse(res.crawlable)
        
    def test_google_form(self):
        res = self.classifier.classify("https://docs.google.com/forms/d/e/1FAIpQLS/viewform", self.options)
        self.assertEqual(res.classification, "google_form")
        self.assertEqual(res.resource_type, "form")
        self.assertEqual(res.handling_mode, HandlingMode.BROWSER)
        self.assertTrue(res.crawlable)
        
    def test_github_repo(self):
        res = self.classifier.classify("https://github.com/torvalds/linux", self.options)
        self.assertEqual(res.classification, "github")
        self.assertEqual(res.handling_mode, HandlingMode.PLATFORM_EXTRACTOR)
        self.assertEqual(res.resource_type, "source_repository")
        
    def test_pdf_file(self):
        res = self.classifier.classify("https://example.com/report.pdf", self.options)
        self.assertEqual(res.classification, "file")
        self.assertEqual(res.resource_type, "file")
        self.assertEqual(res.handling_mode, HandlingMode.DIRECT_DOWNLOAD)
        
    def test_ssrf_protection_localhost(self):
        res = self.classifier.classify("http://127.0.0.1/admin", self.options)
        # Should be blocked
        self.assertEqual(res.matched_rule, "security_blocked")
        self.assertEqual(res.classification, "unknown")
        
    def test_ssrf_protection_metadata(self):
        res = self.classifier.classify("http://169.254.169.254/latest/meta-data", self.options)
        self.assertEqual(res.matched_rule, "security_blocked")

    def test_unsupported_scheme(self):
        res = self.classifier.classify("javascript:alert(1)", self.options)
        self.assertEqual(res.matched_rule, "security_blocked")
        
    @patch('httpx.Client')
    def test_probe_detects_pdf(self, mock_client_class):
        # Mock httpx Client
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        
        # Mock HEAD response
        mock_head = MagicMock()
        mock_head.status_code = 200
        mock_head.is_redirect = False
        mock_head.headers = {"Content-Type": "application/pdf", "Content-Length": "12345"}
        mock_client.head.return_value = mock_head
        
        opts = ClassificationOptions(probe=True, allow_private_network=True)
        res = self.classifier.classify("http://example.com/some_unknown_link", opts)
        
        self.assertTrue(res.probe.performed)
        self.assertEqual(res.classification, "file")
        self.assertEqual(res.resource_type, "document")
        self.assertEqual(res.handling_mode, HandlingMode.DIRECT_DOWNLOAD)
        
    @patch('httpx.Client')
    def test_probe_detects_magic_bytes(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        
        mock_head = MagicMock()
        mock_head.status_code = 200
        mock_head.is_redirect = False
        mock_head.headers = {"Content-Type": "application/octet-stream"}
        mock_client.head.return_value = mock_head
        
        mock_get = MagicMock()
        mock_get.status_code = 206
        mock_get.content = b"PK\x03\x04restofdata"
        mock_client.get.return_value = mock_get
        
        opts = ClassificationOptions(probe=True, allow_private_network=True)
        res = self.classifier.classify("http://example.com/download?id=99", opts)
        
        self.assertTrue(res.probe.performed)
        self.assertEqual(res.probe.magic_type, "zip_based")
        self.assertEqual(res.classification, "file")

if __name__ == "__main__":
    unittest.main()
