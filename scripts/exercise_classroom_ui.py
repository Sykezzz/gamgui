"""Capture and audit the guided Classroom workspaces with Playwright.

This is an opt-in release exercise, not a pytest test. Install Playwright and
its Chromium runtime before invoking it against the local synthetic fixture.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


ROUTES = (
    "/classroom",
    "/classroom/imports",
    "/classroom/courses/manage",
    "/classroom/monitoring",
    "/classroom/recovery",
)
VIEWPORTS = ((1100, 700), (900, 600), (390, 700))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8771")
    parser.add_argument("--out", type=Path, default=Path("dist/verification/classroom"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    findings: list[dict[str, object]] = []
    captures: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        for width, height in VIEWPORTS:
            page = browser.new_page(viewport={"width": width, "height": height})
            errors: list[str] = []
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: errors.append(str(error)))
            for route in ROUTES:
                response = page.goto(f"{args.base_url}{route}", wait_until="networkidle")
                if response is None or response.status >= 400:
                    findings.append({"route": route, "viewport": [width, height], "error": f"HTTP {response.status if response else 'none'}"})
                    continue
                page.locator("details").evaluate_all("items => items.forEach(item => item.open = true)")
                page.wait_for_timeout(100)
                if route == "/classroom/monitoring":
                    receipt_button = page.locator("[data-open-monitoring-receipt]")
                    receipt_button.click()
                    receipt_dialog = page.locator("#monitoring-receipt")
                    receipt_dialog.wait_for(state="visible")
                    if "Technical receipt" not in receipt_dialog.inner_text():
                        findings.append({"route": route, "viewport": [width, height], "control": "technical receipt did not populate"})
                    page.keyboard.press("Escape")
                    receipt_dialog.wait_for(state="hidden")
                    if not receipt_button.evaluate("element => element === document.activeElement"):
                        findings.append({"route": route, "viewport": [width, height], "control": "receipt focus did not return"})

                    pause_button = page.locator("[data-open-monitoring-pause]")
                    pause_button.click()
                    pause_dialog = page.locator("#monitoring-pause-preview")
                    pause_dialog.wait_for(state="visible")
                    if "Open Recovery to pause" not in pause_dialog.inner_text():
                        findings.append({"route": route, "viewport": [width, height], "control": "safe pause preview did not populate"})
                    pause_dialog.locator("[data-close-monitoring-dialog]").click()
                    if not pause_button.evaluate("element => element === document.activeElement"):
                        findings.append({"route": route, "viewport": [width, height], "control": "pause focus did not return"})
                    announcement = page.evaluate(
                        """() => {
                          const live = document.getElementById('monitoring-live');
                          document.body.dispatchEvent(new CustomEvent('htmx:beforeSwap', {detail: {target: live}}));
                          live.dataset.monitorState = 'delayed';
                          live.dataset.monitorAnnouncement = 'The latest local check-in is delayed.';
                          document.body.dispatchEvent(new CustomEvent('htmx:afterSwap', {detail: {target: live}}));
                          live.dataset.monitorState = 'running';
                          return document.getElementById('monitoring-announcer').textContent;
                        }"""
                    )
                    if announcement != "The latest local check-in is delayed.":
                        findings.append({"route": route, "viewport": [width, height], "control": "state transition was not announced"})
                    page.wait_for_timeout(250)
                overflow = page.evaluate(
                    """() => ({
                      client: document.documentElement.clientWidth,
                      scroll: document.documentElement.scrollWidth,
                      bodyChildren: Array.from(document.body.children).map(element => {
                        const box = element.getBoundingClientRect();
                        return {tag: element.tagName, id: element.id, className: String(element.className || ''), left: box.left, right: box.right, scrollWidth: element.scrollWidth, clientWidth: element.clientWidth};
                      }),
                      offenders: Array.from(document.querySelectorAll('*')).map(element => {
                        const box = element.getBoundingClientRect();
                        return {tag: element.tagName, id: element.id, className: String(element.className || ''), left: box.left, right: box.right};
                      }).filter(item => item.right > document.documentElement.clientWidth + 1 || item.left < -1).sort((a, b) => a.right - b.right).slice(0, 12)
                    })"""
                )
                if overflow["scroll"] > overflow["client"] + 1:
                    findings.append({"route": route, "viewport": [width, height], "overflow": overflow})
                route_name = route.strip("/").replace("/", "-") or "root"
                screenshot = args.out / f"{route_name}-{width}x{height}.png"
                page.screenshot(path=str(screenshot), full_page=True)
                captures.append(str(screenshot.resolve()))

                page.locator("body").press("Home")
                page.keyboard.press("Tab")
                focus = page.evaluate("document.activeElement && document.activeElement.tagName")
                if focus not in {"A", "BUTTON", "INPUT", "SELECT", "TEXTAREA", "MAIN"}:
                    findings.append({"route": route, "viewport": [width, height], "keyboard_focus": focus})
            if errors:
                findings.append({"viewport": [width, height], "console_errors": sorted(set(errors))})
            page.close()
        browser.close()

    receipt = {"captures": captures, "findings": findings}
    receipt_path = args.out / "receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps({"receipt": str(receipt_path.resolve()), "captures": len(captures), "findings": findings}, indent=2))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
