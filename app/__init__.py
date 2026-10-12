"""Runs before any app module, so before anything imports the groq SDK.

GROQ_LOG=debug (or info) in the environment makes the SDK log every request it
sends, transcript, prompt and dictionary included, and it reads the variable once,
when it's imported. Blurt's output may be kept in logs\\app.log, so the variable is
removed here, first. (keystore.py does the same for GROQ_BASE_URL and GROQ_CUSTOM_HEADERS.)
"""

import os

os.environ.pop("GROQ_LOG", None)
