"""Click through all three sessions in an iPad-sized browser and save screenshots.

Needs the server running in fast test mode (see README, "Run locally") and the SAMPLE content
(it assumes 4 practice and 4 final questions per text).
Usage: python tests/ui_walk.py [screenshot_dir] [en|ru|ko] [nickname]
Env: BASE_URL (default http://localhost:8077), VIEWPORT=ipad|phone, CHROME_PATH (browser binary).
Exits non-zero if a check fails or the page throws a JavaScript error.
"""
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

import os
BASE = os.environ.get("BASE_URL", "http://localhost:8077")
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "shots")
LANG = sys.argv[2] if len(sys.argv) > 2 else "en"
NICK = sys.argv[3] if len(sys.argv) > 3 else "tester1"
OUT.mkdir(parents=True, exist_ok=True)
LANG_LABEL = {"en": "English", "ru": "Русский", "ko": "한국어"}[LANG]
VIEWPORT = os.environ.get("VIEWPORT", "ipad")
SIZE = {"ipad": {"width": 820, "height": 1180}, "phone": {"width": 390, "height": 844}}[VIEWPORT]


def shot(page, name):
    page.screenshot(path=str(OUT / f"{LANG}_{VIEWPORT}_{name}.png"), full_page=True)


def wait_continue(page):
    btn = page.locator(".actions .btn.primary")
    btn.wait_for(state="visible")
    page.wait_for_function("() => !document.querySelector('.actions .btn.primary').disabled", timeout=20000)
    btn.click()
    page.wait_for_timeout(600)


with sync_playwright() as p:
    exe = os.environ.get("CHROME_PATH")
    browser = p.chromium.launch(executable_path=exe, args=["--no-sandbox"]) if exe else p.chromium.launch()
    ctx = browser.new_context(viewport=SIZE, device_scale_factor=1, has_touch=True, is_mobile=VIEWPORT == "phone")
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE)
    page.get_by_role("button", name=LANG_LABEL).click()
    shot(page, "01_join")
    page.fill("#nick", NICK)
    page.fill("#pin", "1234")
    page.check("#consent")
    page.check("#pledge")
    page.locator("form .btn.primary").click()
    page.wait_for_selector("article.text")
    shot(page, "02_read")
    wait_continue(page)          # read1
    wait_continue(page)          # read2
    page.wait_for_selector(".confs")
    inputs = page.locator(".answer")
    for i in range(inputs.count()):
        inputs.nth(i).fill(["8", "tun", "2006", ""][i] if LANG == "en" else ["8", "x", "2007", ""][i])
        page.locator(".confs").nth(i).locator(".conf").nth([4, 3, 4, 0][i]).click()
    shot(page, "03_test")
    # A reload mid-test must keep typed answers and confidence ratings.
    page.reload()
    page.wait_for_selector(".confs")
    assert page.locator(".answer").first.input_value() == "8", "answers lost after reload"
    assert page.locator(".confs").first.locator(".conf.on").count() == 1, "confidence lost after reload"
    page.locator(".actions .btn.primary").click()
    page.wait_for_selector(".q.ok, .q.bad")
    shot(page, "04_feedback")
    wait_continue(page)          # feedback
    page.wait_for_selector(".big")
    assert page.locator("a.cal").count() == 1, "calendar reminder link missing"
    shot(page, "05_wait")
    time.sleep(6)
    page.reload()
    page.wait_for_selector("article.selectable")
    sents = page.locator(".sent")
    for i in (0, 2, 4):
        sents.nth(i).click()
    shot(page, "06_highlight")
    wait_continue(page)          # hl1
    page.wait_for_selector("article.selectable")
    shot(page, "07_highlight_round2")
    wait_continue(page)          # hl2
    page.wait_for_selector(".confs")
    inputs = page.locator(".answer")
    for i in range(inputs.count()):
        inputs.nth(i).fill("x")
        page.locator(".confs").nth(i).locator(".conf").nth(2).click()
    page.locator(".actions .btn.primary").click()
    page.wait_for_selector(".q.ok, .q.bad")
    wait_continue(page)          # feedback day 2
    page.wait_for_selector("input[type=range]")
    page.locator(".options .chip").first.click()
    page.locator(".actions .btn.primary").click()          # sliders untouched -> must be refused
    page.wait_for_selector(".msg.error")
    assert page.locator("input[type=range]").count() == 2
    ranges = page.locator("input[type=range]")
    ranges.nth(0).fill("3")
    ranges.nth(1).fill("2")
    page.locator(".options .chip").first.click()
    shot(page, "08_predict")
    page.locator(".actions .btn.primary").click()
    page.wait_for_selector(".big")
    time.sleep(6)
    page.reload()
    page.wait_for_selector(".answer")
    shot(page, "09_final")
    for _ in range(2):
        page.wait_for_selector(".answer")
        inputs = page.locator(".answer")
        for i in range(inputs.count()):
            inputs.nth(i).fill(["8", "tun", "2007", "half a mm"][i] if LANG == "en" else "?")
        page.locator(".actions .btn.primary").click()
        page.wait_for_timeout(800)
    page.wait_for_selector(".result")
    shot(page, "10_results")
    print("errors:", errors)
    browser.close()
    sys.exit(1 if errors else 0)
