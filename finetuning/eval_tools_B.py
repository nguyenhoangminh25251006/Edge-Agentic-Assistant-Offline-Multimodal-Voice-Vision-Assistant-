"""Bản cũ là bản sao y hệt eval_tools.py. Giờ trỏ thẳng về eval_tools để hai file không lệch nhau khi sửa."""
import sys

import eval_tools

sys.modules[__name__] = eval_tools
