#!/usr/bin/env python3
"""
Browser helper: read page text and fill forms via AppleScript + JavaScript.
Supports Safari and Google Chrome.

Usage from shell (for Claude Code to call):
  python3 /path/to/browser_helper.py safari        # print Safari page text
  python3 /path/to/browser_helper.py chrome        # print Chrome page text
  python3 /path/to/browser_helper.py url           # print current URL
  python3 /path/to/browser_helper.py fields        # print Safari input fields (JSON)
"""

import subprocess
import logging
import sys


def _run_osascript(script: str, timeout: int = 15) -> str:
    """Execute an AppleScript snippet and return stdout text."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        logging.error("AppleScript error: %s", result.stderr.strip()[:300])
        return f"Error: {result.stderr.strip()[:300]}"
    except subprocess.TimeoutExpired:
        return "Error: AppleScript timeout"
    except Exception as exc:
        return f"Error: {exc}"


# ── Safari ────────────────────────────────────────────────────────────────────

def get_safari_page_text() -> str:
    """Return URL + title + body text of Safari's frontmost tab."""
    script = """
tell application "Safari"
    set pageText to do JavaScript "document.body.innerText" in current tab of front window
    set pageURL to URL of current tab of front window
    set pageTitle to name of current tab of front window
    return "URL: " & pageURL & return & "Title: " & pageTitle & return & return & pageText
end tell
"""
    return _run_osascript(script)


def get_safari_input_fields() -> str:
    """Return JSON array of input fields visible in Safari's frontmost tab."""
    js = (
        "JSON.stringify("
        "  Array.from(document.querySelectorAll('input,select,textarea')).map((el,i) => ({"
        "    index: i,"
        "    type: el.type || el.tagName.toLowerCase(),"
        "    name: el.name || el.id || '',"
        "    placeholder: el.placeholder || '',"
        "    value: el.type === 'password' ? '***' : el.value"
        "  }))"
        ")"
    )
    script = f'tell application "Safari" to do JavaScript "{js}" in current tab of front window'
    return _run_osascript(script)


def get_safari_url() -> str:
    """Return the URL of Safari's current tab."""
    script = 'tell application "Safari" to return URL of current tab of front window'
    return _run_osascript(script)


def safari_open_url(url: str) -> str:
    """Open a URL in Safari."""
    script = f'tell application "Safari" to open location "{url}"'
    return _run_osascript(script)


def safari_run_js(js_code: str) -> str:
    """Run arbitrary JavaScript in Safari's current tab."""
    # Escape double-quotes inside the JS for embedding in AppleScript string
    escaped_js = js_code.replace('\\', '\\\\').replace('"', '\\"')
    script = f'tell application "Safari" to do JavaScript "{escaped_js}" in current tab of front window'
    return _run_osascript(script)


# ── Google Chrome ─────────────────────────────────────────────────────────────

def get_chrome_page_text() -> str:
    """Return URL + title + body text of Chrome's active tab."""
    script = """
tell application "Google Chrome"
    set pageText to execute front window's active tab javascript "document.body.innerText"
    set pageURL to URL of active tab of front window
    set pageTitle to title of active tab of front window
    return "URL: " & pageURL & return & "Title: " & pageTitle & return & return & pageText
end tell
"""
    return _run_osascript(script)


def get_chrome_url() -> str:
    """Return the URL of Chrome's active tab."""
    script = 'tell application "Google Chrome" to return URL of active tab of front window'
    return _run_osascript(script)


def chrome_open_url(url: str) -> str:
    """Open a URL in Chrome."""
    script = f'tell application "Google Chrome" to open location "{url}"'
    return _run_osascript(script)


def chrome_run_js(js_code: str) -> str:
    """Run arbitrary JavaScript in Chrome's active tab."""
    escaped_js = js_code.replace('\\', '\\\\').replace('"', '\\"')
    script = f'tell application "Google Chrome" to execute front window\'s active tab javascript "{escaped_js}"'
    return _run_osascript(script)


# ── Generic helpers ───────────────────────────────────────────────────────────

def get_browser_page_text(browser: str = "safari", max_length: int = 3000) -> str:
    """Get page text from the named browser, truncated to max_length characters."""
    text = get_chrome_page_text() if browser.lower() == "chrome" else get_safari_page_text()
    if len(text) > max_length:
        return text[:max_length] + "\n...(truncated)"
    return text


def get_current_url(browser: str = "safari") -> str:
    """Get current URL from the named browser."""
    return get_chrome_url() if browser.lower() == "chrome" else get_safari_url()


def fill_form_field(field_selector: str, value: str, browser: str = "safari") -> str:
    """
    Fill a form field identified by a CSS selector.
    Returns 'OK' on success or 'NOT_FOUND' if the selector matched nothing.
    """
    val_escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    sel_escaped = field_selector.replace("\\", "\\\\").replace("'", "\\'")

    js = (
        f"(function(){{"
        f"  var el = document.querySelector('{sel_escaped}');"
        f"  if (!el) return 'NOT_FOUND';"
        f"  el.focus();"
        f"  el.value = '{val_escaped}';"
        f"  el.dispatchEvent(new Event('input',{{bubbles:true}}));"
        f"  el.dispatchEvent(new Event('change',{{bubbles:true}}));"
        f"  return 'OK';"
        f"}})();"
    )
    if browser.lower() == "safari":
        return safari_run_js(js)
    return chrome_run_js(js)


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "safari"

    if cmd == "chrome":
        print(get_chrome_page_text())
    elif cmd == "url":
        print(get_current_url())
    elif cmd == "fields":
        print(get_safari_input_fields())
    else:
        print(get_safari_page_text())
