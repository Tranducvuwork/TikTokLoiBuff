"""Wait for visible comment UI without serial waits for each selector."""
import time

VISIBLE_SCRIPT = r"""
return Array.from(document.querySelectorAll(arguments[0])).some(element => {
  const r = element.getBoundingClientRect(), s = getComputedStyle(element);
  return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
});
"""

BUTTONS_SCRIPT = r"""
const seen = new Set();
return Array.from(document.querySelectorAll(arguments[0])).map(element =>
  element.closest('button,a,[role="button"]') || element
).filter(element => {
  if (seen.has(element)) return false;
  seen.add(element);
  const r = element.getBoundingClientRect(), s = getComputedStyle(element);
  return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden'
    && !element.disabled && element.getAttribute('aria-disabled') !== 'true';
});
"""

WAIT_SCRIPT = r"""
const selectors = arguments[0], budget = arguments[1], text = arguments[2];
const done = arguments[arguments.length - 1];
const visible = () => Array.from(document.querySelectorAll(selectors)).some(element => {
  const r = element.getBoundingClientRect(), s = getComputedStyle(element);
  return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden'
    && (!text || (element.textContent || '').trim().length > 0);
});
if (visible()) { done(true); return; }
let finished = false, observer, timer, poll;
const finish = value => {
  if (finished) return;
  finished = true;
  observer?.disconnect(); clearTimeout(timer); clearInterval(poll); done(value);
};
observer = new MutationObserver(() => { if (visible()) finish(true); });
observer.observe(document.documentElement, {subtree:true, childList:true, attributes:true, characterData:true});
timer = setTimeout(() => finish(false), budget);
poll = setInterval(() => { if (visible()) finish(true); }, 150);
if (visible()) finish(true);
"""


def visible(driver, selectors):
    return bool(driver.execute_script(VISIBLE_SCRIPT, selectors))


def buttons(driver, selectors):
    return driver.execute_script(BUTTONS_SCRIPT, selectors) or []


def wait_visible(bot, selectors, timeout=3, require_text=False):
    deadline = time.monotonic() + timeout
    while True:
        bot._check_stop()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        # Short observer windows let cancellation be checked at least once a
        # second, without a WebDriver round trip for every DOM poll.
        if bot.driver.execute_async_script(WAIT_SCRIPT, selectors,
                                           max(1, int(min(1, remaining) * 1000)), require_text):
            bot._check_stop()
            return True
