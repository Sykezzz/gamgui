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
