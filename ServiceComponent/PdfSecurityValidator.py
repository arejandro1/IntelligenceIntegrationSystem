# -*- coding: utf-8 -*-
"""
PdfSecurityValidator PDF安全校验（M5）
文件头校验（%PDF）、大小上限、伪装文件拒绝
"""
import os
from typing import Optional


class PdfSecurityValidator:
    """PDF文件安全校验器（轻量，不执行OCR）"""

    # PDF 魔数头
    PDF_MAGIC = b'%PDF'

    def __init__(self, max_size_bytes: int = 10 * 1024 * 1024,                 # 默认10MB
                 allowed_extensions: Optional[tuple] = None):
        self.max_size_bytes = max_size_bytes
        self.allowed_extensions = allowed_extensions or ('.pdf',)

    def validate(self, file_path: str) -> tuple[bool, str]:
        """
        校验PDF文件安全
        返回 (是否通过, 原因)
        """
        # 1. 文件存在检查
        if not os.path.exists(file_path):
            return False, "文件不存在"

        # 2. 扩展名校验
        _, ext = os.path.splitext(file_path)
        if ext.lower() not in self.allowed_extensions:
            return False, f"不允许的文件扩展名: {ext}"

        # 3. 文件大小检查
        file_size = os.path.getsize(file_path)
        if file_size > self.max_size_bytes:
            return False, f"文件大小({file_size}B)超出上限({self.max_size_bytes}B)"

        if file_size < 4:  # PDF魔数至少4字节
            return False, "文件过小，非有效PDF"

        # 4. 文件头魔数校验（%PDF）
        try:
            with open(file_path, 'rb') as f:
                header = f.read(4)
            if not header.startswith(self.PDF_MAGIC):
                return False, f"文件头校验失败: {header.hex()}"
        except (IOError, PermissionError) as e:
            return False, f"无法读取文件: {e}"

        return True, "通过"

    @staticmethod
    def is_pdf_file(file_path: str) -> bool:
        """快速判断文件是否为PDF（仅文件头校验）"""
        try:
            with open(file_path, 'rb') as f:
                return f.read(4).startswith(b'%PDF')
        except Exception:
            return False