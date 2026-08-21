#!/usr/bin/env python3
"""Check if all required modules are available"""

import sys

modules_to_check = [
    ('PyQt6.QtWidgets', 'PyQt6'),
    ('PyQt6.QtWebEngineWidgets', 'PyQt6-WebEngine'),
    ('PyQt6.QtCore', 'PyQt6'),
    ('PyQt6.QtGui', 'PyQt6'),
    ('requests', 'requests'),
    ('bs4', 'beautifulsoup4'),
    ('psutil', 'psutil'),
    ('telethon', 'telethon'),
    ('google.auth', 'google-auth'),
    ('google.oauth2', 'google-auth'),
    ('google_auth_oauthlib', 'google-auth-oauthlib'),
    ('googleapiclient', 'google-api-python-client'),
    ('dotenv', 'python-dotenv'),
]

missing = []
for module_name, package_name in modules_to_check:
    try:
        __import__(module_name)
        print(f"✓ {module_name}")
    except ImportError as e:
        print(f"✗ {module_name} - Install with: pip install {package_name}")
        missing.append(package_name)

if missing:
    print(f"\nMissing packages: {' '.join(set(missing))}")
    print(f"Install all with: pip install {' '.join(set(missing))}")
    sys.exit(1)
else:
    print("\n✓ All dependencies available!")
    sys.exit(0)
