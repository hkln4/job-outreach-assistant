"""Records the README demo: drives the demo server UI with Playwright,
with a caption bar + fake cursor overlay, and saves a webm video."""
import os

from playwright.sync_api import sync_playwright

BASE = os.path.dirname(os.path.abspath(__file__))
URL = "http://127.0.0.1:8123"
VIDEO_DIR = os.path.join(BASE, "video")

OVERLAY_JS = """
(() => {
  const setup = () => {
    if (document.getElementById('__demo_caption')) return;
    const bar = document.createElement('div');
    bar.id = '__demo_caption';
    Object.assign(bar.style, {
      position: 'fixed', left: '50%', bottom: '14px', transform: 'translateX(-50%)',
      background: 'rgba(15,23,42,0.94)', color: '#fff', padding: '14px 26px',
      borderRadius: '12px', font: '700 23px/1.35 -apple-system, system-ui, sans-serif',
      zIndex: 99999, maxWidth: '92%', textAlign: 'center', pointerEvents: 'none',
      boxShadow: '0 6px 20px rgba(0,0,0,0.4)'
    });
    bar.textContent = sessionStorage.getItem('__demo_caption') || '';
    if (!bar.textContent) bar.style.display = 'none';
    document.body.appendChild(bar);

    const cur = document.createElement('div');
    cur.id = '__demo_cursor';
    Object.assign(cur.style, {
      position: 'fixed', width: '16px', height: '16px', borderRadius: '50%',
      background: 'rgba(37,99,235,0.45)', border: '2.5px solid #2563eb',
      zIndex: 99998, pointerEvents: 'none', transform: 'translate(-50%,-50%)',
      transition: 'left 0.55s cubic-bezier(.4,0,.2,1), top 0.55s cubic-bezier(.4,0,.2,1)',
      left: (sessionStorage.getItem('__demo_cx') || '640') + 'px',
      top: (sessionStorage.getItem('__demo_cy') || '500') + 'px'
    });
    document.body.appendChild(cur);
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', setup);
  } else { setup(); }
})();
"""


def caption(page, text, hold=2600):
    page.evaluate(
        """t => { sessionStorage.setItem('__demo_caption', t);
                 const b = document.getElementById('__demo_caption');
                 if (b) { b.textContent = t; b.style.display = ''; } }""",
        text,
    )
    page.wait_for_timeout(hold)


def move_cursor_to(page, locator):
    locator.scroll_into_view_if_needed()
    page.wait_for_timeout(350)
    box = locator.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.evaluate(
        """([x, y]) => { sessionStorage.setItem('__demo_cx', x); sessionStorage.setItem('__demo_cy', y);
                        const c = document.getElementById('__demo_cursor');
                        if (c) { c.style.left = x + 'px'; c.style.top = y + 'px'; } }""",
        [x, y],
    )
    page.wait_for_timeout(750)


def click(page, locator):
    move_cursor_to(page, locator)
    locator.click()


def type_into(page, locator, text, delay=28):
    move_cursor_to(page, locator)
    locator.click()
    locator.press_sequentially(text, delay=delay)


def smooth_scroll(page, top, wait=1400):
    page.evaluate(f"window.scrollTo({{top: {top}, behavior: 'smooth'}})")
    page.wait_for_timeout(wait)


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            record_video_dir=VIDEO_DIR,
            record_video_size={"width": 1280, "height": 800},
        )
        context.add_init_script(OVERLAY_JS)
        page = context.new_page()

        # 1 - Dashboard
        page.goto(URL + "/")
        caption(page, "Dashboard — one action queue: what to review, draft, research or follow up", 3000)
        smooth_scroll(page, 500)
        smooth_scroll(page, 1000)
        page.wait_for_timeout(600)
        smooth_scroll(page, 0, wait=900)

        # 2 - Profile
        page.goto(URL + "/profile")
        smooth_scroll(page, 430, wait=1100)
        caption(page, "Owner profile — built once from CVs, GitHub & LinkedIn; every skill has evidence", 3400)

        # 3 - Preferences
        page.goto(URL + "/preferences")
        caption(page, "Preferences — hard dealbreakers & soft preferences, checked against every company", 3400)

        # 4 - Add a company
        page.goto(URL + "/companies")
        caption(page, "Add a company — the backend fetches its site and runs research + preference check", 1200)
        form = page.locator("form[action='/companies'][method='post']")
        type_into(page, form.locator("input[name='name']"), "Vektor Analytics")
        type_into(page, form.locator("input[name='website_url']"), "https://vektor-analytics.example", delay=14)
        type_into(page, form.locator("input[name='location']"), "Ankara")
        click(page, form.locator("button[type='submit']"))
        page.wait_for_load_state()

        # 5 - Research results on the company page
        caption(page, "Research done — summary, contact email, suggested people; every finding cites its source URL", 3000)
        smooth_scroll(page, 420)
        page.wait_for_timeout(700)

        # 6 - Draft
        draft_btn = page.locator("form[action$='/draft'] button")
        caption(page, "One click — the LLM picks the position + CV variant and writes a sourced draft", 1400)
        click(page, draft_btn)
        page.wait_for_load_state()
        caption(page, "The draft: AI reasoning, recommended CV, cover letter — company claims backed by sources", 2800)
        smooth_scroll(page, 420)
        smooth_scroll(page, 900)
        page.wait_for_timeout(700)
        smooth_scroll(page, 0, wait=900)

        # 7 - Reject with feedback -> regenerate
        caption(page, "Not happy? Reject with feedback — the draft regenerates until it's right", 1200)
        reject_summary = page.locator("details:has(textarea[name='feedback']) summary")
        click(page, reject_summary)
        page.wait_for_timeout(400)
        fb = page.locator("textarea[name='feedback']")
        type_into(page, fb, "Mention the etl-pipeline project and keep it shorter.", delay=22)
        click(page, page.locator("form:has(textarea[name='feedback']) button[type='submit']"))
        page.wait_for_load_state()
        caption(page, "Regenerated — shorter, and the etl-pipeline project is now front and center", 2600)
        smooth_scroll(page, 450)
        page.wait_for_timeout(600)
        smooth_scroll(page, 0, wait=900)

        # 8 - Approve -> send
        caption(page, "Approve → sent via Gmail with the chosen CV attached. Never auto-sent.", 1400)
        click(page, page.locator("form[action$='/approve']:not(:has(input[name='force'])) button"))
        page.wait_for_load_state()
        caption(page, "Sent — the application is now AWAITING_REPLY and tracked from here on", 2800)

        # 9 - Hard-exclusion block
        page.goto(URL + "/companies/1")
        caption(page, "Preference conflict: a hard exclusion blocks drafting until the owner reviews the evidence", 3600)
        page.wait_for_timeout(400)

        # 10 - Applications lifecycle
        page.goto(URL + "/applications")
        caption(page, "Every application is tracked through its lifecycle — history is never deleted", 3200)

        # 11 - Overdue / escalation queue
        page.goto(URL + "/")
        overdue = page.locator("h2", has_text="No reply yet").first
        overdue.evaluate("e => e.scrollIntoView({behavior: 'smooth', block: 'center'})")
        page.wait_for_timeout(1300)
        caption(page, "No reply after 14 days? Follow-up drafts and employee escalation, from the same queue", 3600)
        page.wait_for_timeout(500)

        video_path = page.video.path()
        context.close()
        browser.close()
        print("VIDEO:", video_path)


if __name__ == "__main__":
    run()
