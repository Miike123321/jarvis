# AGENTS.md — repo knowledge for AI agents

## Project
PyQt6 desktop HUD app (`cyber_panel.py`). Two windows: `CyberPanel` (primary) and
`ExtendedHUD` (second monitor). Single-file app. Repo: https://github.com/Miike123321/jarvis
Work happens on branch `feature/first-run-setup-and-missing-pieces` (PR #1).

## Neon theme (added in the UX/UI redesign)
- Theme constants near top: `NEON_BACKGROUND` (#0a0d10), `NEON_TEXT` (#d9fff8),
  `NEON_MUTED` (#8fd8cc), `ACCENT_CYAN`, `ACCENT_CYAN_DIM`, `ACCENT_ORANGE`, `PANEL_FILL`.
- `HUD_THEME_STYLESHEET` — shared stylesheet used by BOTH windows (do not add per-window
  inline stylesheets; use this + `panel_style()`).
- `panel_style(object_name=None, pad=10)` — helper for rounded neon-edged panels.
- Custom-painted widgets: `RadialIndicator` (BTC/UAH gauges), `HudGauge` (CPU/RAM),
  `NewsCarousel` (ticker), `VideoCard` (thumbnail+PLAY), sphere globe.
- The globe is `NeonSphereWidget(...)` — a FACTORY, not a class. It returns
  `GLSphereWidget` (QOpenGLWidget) when PyOpenGL/GL is available, else
  `SoftwareSphereWidget` (pure QPainter). OpenGL is OPTIONAL: imports are wrapped
  in try/except (`_GL_AVAILABLE`), so the app starts with no PyOpenGL installed.
  Shared point-cloud/timer logic lives in `_SphereBase`.

## Building / running on the user's machine
- Windows: `build_exe.bat` (PyInstaller) — user builds the .exe themselves.
- Python deps in `requirements.txt` (includes `PyOpenGL` for the globe).

## Verifying renders in THIS environment (hard-won)
- **The sandbox resets between sessions** — PyQt6, xvfb, and the X/GL system libs
  may ALL vanish. If `xvfb-run`/`PyQt6`/`libEGL` are missing, re-run the installs below.
  Reinstall python deps: `pip install PyQt6 PyQt6-WebEngine psutil requests
  beautifulsoup4 python-dotenv telethon google-auth google-auth-oauthlib
  google-api-python-client` (and `PyOpenGL` for the GL globe path).
- PyQt6 periodically gets dropped → `pip install` it again if `ModuleNotFoundError`.
- **Offscreen (no GL):** `QT_QPA_PLATFORM=offscreen`. Works for QWidget painting
  (gauges, cards, ring) but NOT for QOpenGLWidget (GL context invalid / grabFramebuffer fails).
- **OpenGL (the globe) needs a real X server:** use `xvfb-run -a python3 ...`.
  Requires system packages (installed via `sudo -n apt-get install -y`):
  `xvfb libegl1 libgl1 libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0
  libxcb-keysyms1 libxcb-render-util0 libxcb-randr0 libxcb-shape0 libxcb-xinerama0
  libxcb-xfixes0 libxcb-sync1 libxcb-shm0 libxcb-render0 libxcb-glx0`.
- Verify GL with `w.grabFramebuffer().save(path)` on the QOpenGLWidget (NOT `widget.grab()`,
  which is black for GL). Whole-window `grab()` still works under Xvfb.
- `gluPerspective` is UNAVAILABLE (core context) — build the projection matrix manually
  with `glLoadMatrixf` (see `NeonSphereWidget.resizeGL`).
- `ExtendedHUD` widget for the carousel is `ext_news_carousel` (not `news_carousel`).

## Testing conventions
- Throwaway render/verify scripts go in `/tmp`, are deleted after use, and print a
  single OK marker. Render PNGs to /tmp and view them to confirm visuals.
- Always `python3 -m py_compile cyber_panel.py` before rendering.

## Git
- Commit with `Co-authored-by: openhands <openhands@all-hands.dev>`.
- If `git push` prompts for a password, the embedded token expired:
  `git remote set-url origin "https://${GITHUB_TOKEN}@github.com/Miike123321/jarvis.git"`.
