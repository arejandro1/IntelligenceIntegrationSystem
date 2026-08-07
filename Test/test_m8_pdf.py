# -*- coding: utf-8 -*-
"""M8: PDF 安全校验测试——文件头/大小/伪装"""
import os
import tempfile
import unittest
from ServiceComponent.PdfSecurityValidator import PdfSecurityValidator


class TestPDFSecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = PdfSecurityValidator(max_size_bytes=1024)

    def test_valid_pdf_header(self):
        f = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
        f.write(b'%PDF-1.4\nSome content here')
        f.close()
        ok, msg = self.validator.validate(f.name)
        os.unlink(f.name)
        self.assertTrue(ok, msg)

    def test_fake_pdf_header(self):
        f = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
        f.write(b'FAKE header not pdf')
        f.close()
        ok, msg = self.validator.validate(f.name)
        os.unlink(f.name)
        self.assertFalse(ok)
        self.assertIn('文件头', msg)

    def test_oversize_pdf(self):
        f = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
        f.write(b'%PDF-1.4\n' + b'x' * 2000)  # > 1024 bytes
        f.close()
        ok, msg = self.validator.validate(f.name)
        os.unlink(f.name)
        self.assertFalse(ok)
        self.assertIn('超出上限', msg)

    def test_wrong_extension(self):
        f = tempfile.NamedTemporaryFile(suffix='.txt', delete=False)
        f.write(b'%PDF-1.4\ncontent')
        f.close()
        ok, msg = self.validator.validate(f.name)
        os.unlink(f.name)
        self.assertFalse(ok)
        self.assertIn('不允许', msg)

    def test_too_small_file(self):
        f = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
        f.write(b'ABC')
        f.close()
        ok, msg = self.validator.validate(f.name)
        os.unlink(f.name)
        self.assertFalse(ok, msg)

    def test_static_is_pdf(self):
        f = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
        f.write(b'%PDF-1.5\nmore data')
        f.close()
        result = PdfSecurityValidator.is_pdf_file(f.name)
        os.unlink(f.name)
        self.assertTrue(result)

    def test_static_not_pdf(self):
        f = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
        f.write(b'NOT A PDF FILE')
        f.close()
        result = PdfSecurityValidator.is_pdf_file(f.name)
        os.unlink(f.name)
        self.assertFalse(result)


if __name__ == '__main__':
    unittest.main()