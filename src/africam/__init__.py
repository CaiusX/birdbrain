"""Top-level package.

This module is imported before any of our submodules (and therefore before
TensorFlow / birdnetlib / pydub), so it's the right place to silence the
chatty third-party imports that we don't control.
"""
from __future__ import annotations

import os
import warnings

# TensorFlow C++ logger: 0=all, 1=INFO, 2=WARNING, 3=ERROR. Set BEFORE TF imports.
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
# Silence the "oneDNN custom operations are on" startup notice.
os.environ.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")
# Stop absl from logging to stderr before InitializeLog() is called.
os.environ.setdefault("GLOG_minloglevel", "2")

# pydub ships regexes with unescaped parens; this fires SyntaxWarning at import time.
warnings.filterwarnings("ignore", category=SyntaxWarning, module=r"pydub\..*")
# tf.lite.Interpreter prints a UserWarning on every construction in TF >=2.16.
warnings.filterwarnings("ignore", message=r".*tf\.lite\.Interpreter is deprecated.*")

__version__ = "0.1.0"
