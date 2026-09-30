import unittest

from app.url_security import UnsafeSurveyUrl, validate_public_url


class UrlSecurityTests(unittest.TestCase):
    def test_rejects_localhost(self):
        with self.assertRaises(UnsafeSurveyUrl):
            validate_public_url("http://localhost:8000/test")

    def test_rejects_non_http_scheme(self):
        with self.assertRaises(UnsafeSurveyUrl):
            validate_public_url("file:///etc/passwd")


if __name__ == "__main__":
    unittest.main()
