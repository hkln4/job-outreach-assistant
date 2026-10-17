"""Records the README demo: drives the demo server UI with Playwright, with a
short caption bar + a cursor dot, and saves a webm video (see README.md here)."""
import os
import re

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
      position: 'fixed', left: '50%', bottom: '18px', transform: 'translateX(-50%)',
      background: 'rgba(17,24,39,0.86)', color: '#fff', padding: '9px 18px',
      borderRadius: '8px', font: '500 17px/1.4 -apple-system, system-ui, sans-serif',
      zIndex: 99999, maxWidth: '80%', textAlign: 'center', pointerEvents: 'none'
    });
    bar.textContent = sessionStorage.getItem('__demo_caption') || '';
    if (!bar.textContent) bar.style.display = 'none';
    document.body.appendChild(bar);

    const cur = document.createElement('div');
    cur.id = '__demo_cursor';
    Object.assign(cur.style, {
      position: 'fixed', width: '14px', height: '14px', borderRadius: '50%',
      background: 'rgba(79,70,229,0.35)', border: '2px solid #4f46e5',
      zIndex: 99998, pointerEvents: 'none', transform: 'translate(-50%,-50%)',
      transition: 'left 0.5s cubic-bezier(.4,0,.2,1), top 0.5s cubic-bezier(.4,0,.2,1)',
      left: (sessionStorage.getItem('__demo_cx') || '640') + 'px',
      top: (sessionStorage.getItem('__demo_cy') || '400') + 'px'
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


def point_at(page, locator, wait=700):
    locator.scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    box = locator.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.evaluate(
        """([x, y]) => { sessionStorage.setItem('__demo_cx', x); sessionStorage.setItem('__demo_cy', y);
                        const c = document.getElementById('__demo_cursor');
                        if (c) { c.style.left = x + 'px'; c.style.top = y + 'px'; } }""",
        [x, y],
    )
    page.wait_for_timeout(wait)


def click(page, locator):
    point_at(page, locator)
    locator.click()


def type_into(page, locator, text, delay=30):
    point_at(page, locator, wait=400)
    locator.click()
    locator.press_sequentially(text, delay=delay)


def scroll_to(page, locator, wait=1200):
    locator.evaluate("e => e.scrollIntoView({behavior: 'smooth', block: 'start'})")
    page.wait_for_timeout(wait)


def scroll_by(page, dy, wait=1200):
    page.evaluate(f"window.scrollBy({{top: {dy}, behavior: 'smooth'}})")
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
        caption(page, "Dashboard: everything that needs you, in one place", 3000)

        # 2 - Profile
        page.goto(URL + "/profile")
        caption(page, "Profile: built once from your CVs, GitHub repos and LinkedIn", 2200)
        scroll_by(page, 450)
        caption(page, "Every skill points to the project or CV it came from", 2200)

        # 3 - Preferences
        page.goto(URL + "/preferences")
        caption(page, "Preferences: dealbreakers are checked against every company", 2800)

        # 4 - Discover: import a directory, screen it, decide
        page.goto(URL + "/discovery")
        caption(page, "Discover: find companies in technopark directories", 2200)
        click(page, page.locator("form[action$='/import'] button").first)
        page.wait_for_load_state()
        caption(page, "The directory's companies are imported as candidates", 2200)
        click(page, page.locator("a.button-link[href*='triage/step']").first)
        page.wait_for_url(re.compile(r"/discovery\?source_id=\d+$"), timeout=30000)
        page.wait_for_load_state()
        table = page.locator("table.discovery-table")
        scroll_to(page, table)
        caption(page, "Each homepage is screened: what they do, how well they fit", 3000)
        atlas = page.locator("tr", has_text="ATLAS")
        point_at(page, atlas.locator(".badge-hard"))
        caption(page, "Dealbreakers are flagged, with the words from their site", 2800)
        click(page, page.locator("tr", has_text="ORBİS").locator("button.decide-yes"))
        caption(page, "✓ adds a company to your list", 1800)
        click(page, atlas.locator("button.decide-no"))
        caption(page, "✕ hides it for good", 1800)
        lumen = page.locator("tr", has_text="LUMEN")
        point_at(page, lumen.locator("a", has_text="Google"))
        caption(page, "No website listed? Search for it and paste it in", 2600)

        # 5 - Add a company by hand -> research runs
        page.goto(URL + "/companies")
        caption(page, "Or add a company yourself", 1200)
        form = page.locator("#add-company-form")
        type_into(page, form.locator("input[name='name']"), "Vektor Analytics")
        type_into(page, form.locator("input[name='website_url']"), "https://vektor-analytics.example", delay=12)
        type_into(page, form.locator("input[name='location']"), "Ankara")
        click(page, form.locator("button[type='submit']"))
        caption(page, "Its website is researched right away", 1600)
        link = page.locator("table a", has_text="Vektor Analytics").first
        link.wait_for(timeout=15000)
        click(page, link)
        page.wait_for_load_state()

        # 6 - Research on the company page
        scroll_to(page, page.locator("h2", has_text="Research"))
        caption(page, "Research: what they do, open roles, contact email, all with sources", 3200)
        scroll_to(page, page.locator("h1"), wait=900)

        # 7 - Draft
        click(page, page.locator("details.draft-toggle > summary"))
        page.wait_for_timeout(500)
        caption(page, "Draft an application", 1200)
        click(page, page.locator("form[action$='/draft'] button[type='submit']"))
        page.wait_for_url(re.compile(r"/applications/\d+$"), timeout=30000)
        page.wait_for_load_state()
        caption(page, "AI notes explain the position and CV it picked", 2800)
        scroll_to(page, page.locator(".email-card"))
        caption(page, "The letter is built on your real projects and their site", 3000)
        scroll_to(page, page.locator("#evidence"))
        caption(page, "Profile evidence: which of your projects the letter uses", 3000)

        # 8 - Reject with feedback -> rewrite
        scroll_to(page, page.locator(".review-panel"), wait=900)
        click(page, page.locator("details.reject-toggle > summary"))
        page.wait_for_timeout(300)
        type_into(page, page.locator("textarea[name='feedback']"), "Daha kısa olsun, etl-pipeline öne çıksın.", delay=24)
        caption(page, "Not right? Say what to change", 1200)
        click(page, page.locator("form[action$='/reject'] button[type='submit']"))
        page.wait_for_load_state()
        scroll_to(page, page.locator(".email-card"))
        caption(page, "Rewritten with your feedback", 2600)

        # 9 - Approve -> send
        scroll_to(page, page.locator(".review-panel"), wait=900)
        caption(page, "Approve: sent from your Gmail with the right CV. Never automatic.", 1800)
        click(page, page.locator(".review-approve-form button"))
        page.wait_for_load_state()
        caption(page, "Sent, and tracked until they reply", 2600)

        # 10 - A dealbreaker company
        page.goto(URL + "/companies/1")
        caption(page, "A dealbreaker match blocks drafting until you decide", 3200)

        # 11 - Applications history
        page.goto(URL + "/applications")
        caption(page, "Applications: every one you sent, with its state", 2800)

        # 12 - Follow-up queue
        page.goto(URL + "/")
        queue = page.locator("h2", has_text="No reply yet").first
        scroll_to(page, queue)
        caption(page, "No reply after 14 days? Follow up, or write to someone at the company", 3400)

        video_path = page.video.path()
        context.close()
        browser.close()
        print("VIDEO:", video_path)


if __name__ == "__main__":
    run()
