"""Tests for catalog module"""

import unittest
from app.catalog import _extract_object_ids


class TestExtractObjectIds(unittest.TestCase):
    """Test the _extract_object_ids function with various filename patterns"""

    def test_extract_object_ids(self):
        """Test various filename patterns"""
        test_cases = [
            # Basic patterns
            ("M 31", ["M31"], "Space between prefix and number"),
            ("M_31", ["M31"], "Underscore between prefix and number"),
            ("M_31_stretched", ["M31"], "Underscore separator with suffix"),
            ("M31_stretched", ["M31"], "No separator with suffix"),
            ("M31 stretched", ["M31"], "No separator with space suffix"),
            ("M31", ["M31"], "Simple case"),
            ("M0031", ["M31"], "Leading zeros"),
            ("M 0031", ["M31"], "Space with leading zeros"),
            
            # Different catalogs
            ("NGC1234", ["NGC1234"], "NGC catalog"),
            ("NGC 1234", ["NGC1234"], "NGC with space"),
            ("NGC_1234", ["NGC1234"], "NGC with underscore"),
            ("IC0001", ["IC1"], "IC catalog with leading zeros"),
            ("C42", ["C42"], "Caldwell catalog"),
            
            # Filename contexts
            ("photo-M31-final", ["M31"], "Object ID in middle of filename"),
            
            # Edge cases - should NOT match (word boundary protection)
            ("AM31", [], "Should not match M31 inside AM31"),
            ("M3145", ["M3145"], "Should match M3145 as valid 4-digit object ID"),
            ("XM31", [], "Should not match M31 inside XM31"),
            ("M31A", [], "Should not match M31 when followed by letter"),
            ("", [], "Empty string"),
            ("no_match_here", [], "No catalog object"),
        ]
        
        for input_str, expected, description in test_cases:
            with self.subTest(input_str=input_str, description=description):
                result = _extract_object_ids(input_str)
                self.assertEqual(
                    result,
                    expected,
                    f"{description}: Expected {expected}, got {result} for input '{input_str}'"
                )


if __name__ == "__main__":
    unittest.main()
