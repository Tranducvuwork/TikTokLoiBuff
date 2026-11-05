#!/usr/bin/env python3
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.path.join(BASE, 'runtime')
sys.path.insert(0, RUNTIME)

os.chdir(RUNTIME)

from client.app.bridge import Bridge
from client.app.runner import Runner

import tiktok_reply  # triggers all install() chains — monkey-patches Bridge & Runner

import webview

bridge = Bridge()
html_path = os.path.join(RUNTIME, 'client', 'web', 'index.html')

window = webview.create_window(
    'Auto-Like-Cmt v95',
    url=html_path,
    js_api=bridge,
    width=1100,
    height=750,
    min_size=(900, 600),
    text_select=True,
)
bridge._window = window
webview.start(debug=('--debug' in sys.argv))
