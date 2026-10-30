"""Anonymous native document navigation with guarded redirects and bounded resources."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Request, Route, async_playwright

from .core import Failure, Run, utc
from .extraction import caption_index, merge_public_snapshots, public_snapshot
from .security import PROFILE, NavigationGuard, check_url, public_dns

THREAD_REGION_SELECTOR = 'section[aria-label="Replies"],[role="region"][aria-label="Replies"],section[aria-label="回覆"],[role="region"][aria-label="回覆"]'


VISIBLE_POSTS_JS = """(known) => {
  const selector='article,[data-pressable-container],div[data-interactive-id]';
  const cards=[...document.querySelectorAll(selector)].filter(e=>e.getClientRects().length);
  const result=cards.slice(0,80).map(card=>{
    const own=e=>e.closest(selector)===card;
    const links=[...card.querySelectorAll('a[href*="/post/"]')].filter(own);
    const link=links.find(a=>a.querySelector('time')) || links[0];
    const blocks=[...card.querySelectorAll('[dir="auto"]')].filter(e=>own(e) && e.getClientRects().length &&
      !e.closest('a,button,time,header,nav,footer') && !e.querySelector('time,button'));
    const outer=blocks.filter(e=>!blocks.some(other=>other!==e && other.contains(e)));
    const reply=card.querySelector('a[rel~="in-reply-to"],a[aria-label^="Replying to"],a[aria-label^="回覆"]');
    const name=card.querySelector('h2,h3');
    return {url:link?.href??null,text:outer.length===1?outer[0].innerText:null,
      parent_url:reply?.href??null,display_name:name?.innerText??null,
      truncated:[...card.querySelectorAll('button,[role="button"]')].some(e=>/^(see more|read more|顯示更多|更多)$/i.test((e.innerText||'').trim()))};
  });
  // Some public pages have no article/pressable wrapper. Anchor the body to its
  // own visible timestamp permalink, then use the smallest unambiguous container.
  const identity=url=>{try{const u=new URL(url);return /^(www\\.)?threads\\.(com|net)$/.test(u.hostname)?u.pathname.match(/^\\/@[^/]+\\/post\\/[^/]+\\/?$/)?.[0].replace(/\\/$/,''):null;}catch{return null;}};
  const norm=t=>(t||'').replace(/\\s+/g,' ').trim();
  for(const anchor of [...document.querySelectorAll('a[href*="/post/"]')].filter(a=>a.querySelector('time')&&a.getClientRects().length).slice(0,80)){
    const id=identity(anchor.href);if(!id)continue;
    for(let card=anchor.parentElement,depth=0;card&&depth<12;card=card.parentElement,depth++){
      if(card===document.body)break;
      const ids=new Set([...card.querySelectorAll('a[href*="/post/"]')].map(a=>identity(a.href)).filter(Boolean));
      if(ids.size!==1||!ids.has(id))break;
      const blocks=[...card.querySelectorAll('[dir="auto"]')].filter(e=>e.getClientRects().length&&
        !e.closest('a,button,[role="button"],time,header,nav,footer')&&!e.querySelector('time,button,[role="button"]'));
      const outer=blocks.filter(e=>!blocks.some(other=>other!==e&&other.contains(e)));
      let body=null,evidence=null;
      if(outer.length===1&&outer[0].innerText.trim()){body=outer[0];evidence='permalink_time_scoped_visible_body';}
      else if(outer.length>1&&known&&typeof known[id]==='string'){
        // Reply cards carry name/badge rows beside the body. Accept only the single block whose
        // text equals the public caption for this permalink; anything else is not a body claim.
        const match=outer.filter(e=>norm(e.innerText)===norm(known[id]));
        if(match.length===1){body=match[0];evidence='permalink_time_scoped_caption_match';}
      }
      if(!body)continue;
      const more=[...card.querySelectorAll('button,[role="button"]')].some(e=>/^(see more|read more|顯示更多|更多)$/i.test((e.innerText||'').trim()));
      let clipped=false;
      for(let e=body;e;e=e.parentElement){
        const style=getComputedStyle(e);
        if(parseInt(style.webkitLineClamp)>0||(['hidden','clip'].includes(style.overflowY)&&e.scrollHeight>e.clientHeight+1))clipped=true;
        if(e===card)break;
      }
      const truncated=more||clipped;
      result.push({url:anchor.href,text:body.innerText,truncated,parent_url:null,display_name:null,
        body_evidence:evidence,inline_image_count:body.querySelectorAll('img').length});
      break;
    }
  }
  return result;
}"""

MORE_REPLIES_PATTERN = (
    r"^(load more replies|show more replies|view more replies|載入更多回覆|顯示更多回覆|查看更多回覆)$"
)


class Browser:
    def __init__(
        self,
        run: Run,
        transport: Callable[[str], Awaitable[tuple]] | None = None,
        dns: Callable[[str], Awaitable[None]] = public_dns,
        settle_ms: int = 1200,
    ) -> None:
        self.run = run
        self.transport = transport  # deterministic browser-level tests only
        self.dns = dns
        self.settle_ms = settle_ms
        self.pw = self.browser = self.context = self.page = None
        self.guard = None
        self.pending = None
        self.failure = None
        self.statuses = []
        self.resource_count = 0
        self.snapshot = {}
        self.navigation_ticket = None
        self.diagnostics = {"counts": {}, "requests": [], "details_omitted": 0, "observation": None}
        self.document_cdp = None
        self.main_frame_id = None
        self.document_tasks = set()

    def diagnose(self, reason: str, req: Request | None = None, status: int | None = None) -> None:
        d = self.diagnostics
        d["counts"][reason] = d["counts"].get(reason, 0) + 1
        detail = {"reason_code": reason}
        if req:
            u = urlsplit(req.url)
            detail.update(method=req.method, resource_type=req.resource_type, host=u.hostname, path=u.path)
        if status is not None:
            detail["http_status"] = status
        if len(d["requests"]) < 40:
            d["requests"].append(detail)
        else:
            d["details_omitted"] += 1

    async def block(self, route: Route, reason: str) -> None:
        self.diagnose(reason, route.request)
        await route.abort()

    async def observe(self) -> None:
        # Hydration observation is bounded and never evidence of thread completeness.
        budget = min(
            self.settle_ms if self.transport else 5000, self.guard.remaining() * 1000, self.run.remaining() * 1000
        )
        try:
            await self.page.wait_for_function(
                """() => document.readyState !== 'loading' && (
              document.querySelector('link[rel="canonical"]') ||
              (document.body?.innerText || '').trim().length > 0 ||
              document.querySelector('form[action*="/challenge"],iframe[src*="captcha"],form[action*="/accounts/login"]'))""",
                timeout=max(1, budget),
            )
            self.diagnostics["observation"] = "observable_page_ready"
        except PlaywrightError:
            self.diagnostics["observation"] = "hydration_unconfirmed"
        await self.page.wait_for_timeout(min(50, self.guard.remaining() * 1000))

    async def start(self) -> None:
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch()
        self.context = await self.browser.new_context(service_workers="block", accept_downloads=False)
        await self.context.route("**/*", self._route)
        await self.context.route_web_socket("**/*", lambda ws: ws.close())
        self.page = await self.context.new_page()
        if self.transport is None:
            # Pause each document response before Chromium follows HTTP Location.
            # Ordinary Playwright routing alone need not re-enter on an HTTP redirect.
            self.document_cdp = await self.context.new_cdp_session(self.page)
            self.main_frame_id = (await self.document_cdp.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]

            def paused(event):
                task = asyncio.create_task(self._document_response(event))
                self.document_tasks.add(task)
                task.add_done_callback(self.document_tasks.discard)

            self.document_cdp.on("Fetch.requestPaused", paused)
            await self.document_cdp.send(
                "Fetch.enable", {"patterns": [{"resourceType": "Document", "requestStage": "Response"}]}
            )
        self.page.on("popup", lambda p: asyncio.create_task(p.close()))
        self.page.on("download", lambda d: asyncio.create_task(d.cancel()))
        self.page.on("dialog", lambda d: asyncio.create_task(d.dismiss()))
        self.page.on("requestfailed", lambda req: self.diagnose("request_failed", req))
        self.page.on("pageerror", lambda error: self.diagnose("page_script_error"))

    async def _document_response(self, event: dict) -> None:
        request_id = event["requestId"]
        try:
            if self.failure:
                raise self.failure
            if event["frameId"] != self.main_frame_id:
                raise Failure("redirect_target_blocked")
            self.run.remaining()
            self.guard.remaining()
            # Chromium requests the raw Location; the guard holds the normalized form
            # (lower-cased host, tracking parameters removed), so compare normalized.
            url = check_url(event["request"]["url"], self.guard.profile)
            if url != self.guard.current:
                raise Failure("redirect_target_blocked")
            if self.navigation_ticket == url:
                self.navigation_ticket = None
            status = event.get("responseStatusCode")
            if status is None:
                raise Failure("browser_network_error", technical=True)
            self.statuses.append({"url": url, "status": status})
            if status in {401, 403, 429}:
                raise Failure(
                    "login_required" if status == 401 else "access_denied" if status == 403 else "rate_limited"
                )
            if status >= 400:
                raise Failure("share_unavailable" if "/share/" in url else "source_unavailable")
            if 300 <= status < 400:
                location = next(
                    (h["value"] for h in event.get("responseHeaders", []) if h["name"].lower() == "location"), None
                )
                if not location:
                    raise Failure("unresolved_target")
                target = self.guard.follow(location, "http_redirect")
                await self.dns(target)
                # Single-use authorization, whether or not Playwright routes the next hop.
                self.navigation_ticket = target
            await self.document_cdp.send("Fetch.continueResponse", {"requestId": request_id})
        except Exception as error:
            self.failure = error if isinstance(error, Failure) else Failure("document_guard_error", technical=True)
            self.diagnose(self.failure.code)
            try:
                await self.document_cdp.send(
                    "Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"}
                )
            except PlaywrightError:
                pass

    async def _route(self, route: Route) -> None:
        req = route.request
        main = req.is_navigation_request() and req.frame == self.page.main_frame
        try:
            self.run.remaining()
            if self.failure:
                return await self.block(route, "run_stopped")
            if req.method != "GET":
                return await self.block(route, "policy_non_get_blocked")
            if req.is_navigation_request() and not main:
                return await self.block(route, "policy_frame_blocked")
            if main:
                self.guard.remaining()
                try:
                    normalized = check_url(req.url, self.guard.profile)
                except Failure:
                    normalized = None
                if normalized is not None and self.navigation_ticket == normalized:
                    self.navigation_ticket = None
                else:
                    self.guard.follow(req.url, "browser_navigation")
                check_url(req.url, self.guard.profile)
            else:
                self.resource_count += 1
                if self.resource_count > 150 or req.resource_type not in {"script", "stylesheet", "fetch", "xhr"}:
                    return await self.block(route, "policy_resource_budget_or_type")
                check_url(req.url, resource=True)
            await self.dns(req.url)
            timeout = int(min(self.run.remaining(), self.guard.remaining(), 15) * 1000)
            if main and self.transport is None:
                # Keep the browser's document request/response, rather than replaying via APIRequest.
                # _document_response holds every HTTP redirect until URL and DNS checks pass.
                return await route.continue_()
            if self.transport:
                response = await self.transport(req.url)
                status, headers, body = response
            else:
                response = await route.fetch(max_redirects=0, max_retries=0, timeout=max(1, timeout))
                status, headers, body = response.status, response.headers, await response.body()
            if main:
                self.statuses.append({"url": self.guard.current, "status": status})
                if status in {401, 403, 429}:
                    raise Failure(
                        "login_required" if status == 401 else "access_denied" if status == 403 else "rate_limited"
                    )
                if status >= 400:
                    raise Failure("share_unavailable" if "/share/" in req.url else "source_unavailable")
            elif status >= 400:
                self.diagnose("resource_http_error", req, status)
            if 300 <= status < 400:
                if main:
                    if not headers.get("location"):
                        raise Failure("unresolved_target")
                    self.pending = self.guard.follow(headers["location"], "http_redirect")
                # Resource redirects are blocked instead of followed outside interception.
                return await self.block(route, "http_redirect_reissued" if main else "policy_resource_redirect_blocked")
            if len(body) > 8_000_000:
                if main:
                    raise Failure("source_too_large")
                return await self.block(route, "policy_resource_size_blocked")
            safe_headers = {
                k: v
                for k, v in headers.items()
                if k.lower()
                not in {
                    "set-cookie",
                    "content-encoding",
                    "content-length",
                    "transfer-encoding",
                    "connection",
                    "location",
                }
            }
            await route.fulfill(status=status, headers=safe_headers, body=body)
        except Exception as e:
            self.diagnose(e.code if isinstance(e, Failure) else "resource_transport_error", req)
            if main:
                self.failure = e if isinstance(e, Failure) else Failure("browser_network_error", technical=True)
            try:
                await route.abort()
            except PlaywrightError:
                pass

    async def navigate(self, url: str, profile: str | None = None) -> dict:
        if not self.page:
            await self.start()
        self.guard = NavigationGuard(url, profile, min(30, self.run.remaining()))
        self.pending, self.failure = None, None
        try:
            target = url
            while True:
                self.pending = None
                self.navigation_ticket = target  # one-shot initial/manual HTTP-hop authorization
                try:
                    await self.page.goto(
                        target,
                        wait_until="domcontentloaded",
                        timeout=int(min(self.guard.remaining(), self.run.remaining()) * 1000),
                    )
                    await self.observe()
                except PlaywrightError as navigation_error:
                    # A real meta/JS navigation can interrupt goto before its route callback finishes.
                    # Yield to that callback before deciding this is a transport failure.
                    await self.page.wait_for_timeout(min(self.settle_ms, self.guard.remaining() * 1000))
                    if not self.pending and not self.failure:
                        if "interrupted by another navigation" not in str(navigation_error):
                            raise Failure("browser_navigation_error", technical=True)
                if self.failure:
                    raise self.failure
                if self.pending:
                    target = self.pending
                    continue
                break
            self.guard.remaining()
            doc = await self.page.evaluate("""() => ({
              canonical: document.querySelector('link[rel="canonical"]')?.href ?? null,
              scripts: [...document.querySelectorAll('script[type="application/json"],script[type="application/ld+json"]')]
                .filter(e => e.textContent.length <= 2000000).slice(0,100).map(e => e.textContent),
              blocked: [...document.querySelectorAll('form[action*="/challenge"],iframe[src*="captcha"],form[action*="/accounts/login"]')]
                .some(e => e.getClientRects().length > 0),
              challenge: [...document.querySelectorAll('form[action*="/challenge"],iframe[src*="captcha"]')].some(e=>e.getClientRects().length>0),
              visible_text_length: (document.body?.innerText || '').length,
              ready_state: document.readyState,
              title: document.title
            })""")
            doc["visible_posts"] = await self.page.evaluate(VISIBLE_POSTS_JS, caption_index(doc["scripts"]))
            doc["thread_observed"] = await self.page.locator(THREAD_REGION_SELECTOR).evaluate_all(
                "els=>els.some(e=>e.getClientRects().length>0)"
            )
            doc["has_more"] = await self.page.locator("button").evaluate_all(
                "els=>els.some(e=>e.getClientRects().length && /^(load more replies|show more replies|載入更多回覆|顯示更多回覆)$/i.test((e.innerText||'').trim()))"
            )
            # A non-obstructing login invitation beside a visible post is not a login wall.
            if (
                doc["challenge"]
                or doc["blocked"]
                and not any(isinstance(p.get("text"), str) for p in doc["visible_posts"])
            ):
                raise Failure("login_or_challenge")
            if urlsplit(self.page.url).hostname not in {
                "threads.com",
                "www.threads.com",
                "threads.net",
                "www.threads.net",
            }:
                raise Failure("redirect_target_blocked")
            return doc
        except Failure as e:
            self.failure = e
            raise
        except PlaywrightError as e:
            # A blocked JS navigation can destroy the observation context just before
            # its route callback records the loop. Yield, then preserve that exact failure.
            await self.page.wait_for_timeout(min(100, self.guard.remaining() * 1000))
            self.failure = self.failure or Failure("browser_observation_error", technical=True)
            raise self.failure from e
        finally:
            self.run.event(
                "navigation",
                input={"url": url},
                output={
                    "redirect_chain": self.guard.chain,
                    "final_url": self.guard.current,
                    "http_statuses": self.statuses,
                    "acquisition_diagnostics": self.diagnostics,
                },
                status="error" if self.failure else "observed",
                reason_code=self.failure.code if self.failure else None,
            )
            # Preserve honest resolution evidence even when extraction has no output.
            if not profile:
                self.run.snapshot.update(final_url=self.guard.current, redirect_chain=list(self.guard.chain))

    async def read_post(self) -> dict:
        doc = await self.navigate(self.run.source)
        captured = utc()
        self.snapshot = public_snapshot(doc, self.run.source, self.page.url, list(self.guard.chain), captured)
        self.snapshot["acquisition_diagnostics"] = {
            **self.diagnostics,
            "visible_text_length": doc["visible_text_length"],
            "json_script_count": len(doc["scripts"]),
            "ready_state": doc["ready_state"],
        }
        # A successful HTTP response is evidence of a page, not of requested source content.
        if self.snapshot.get("canonical_url"):
            self.run.captured_at = captured
        else:
            self.run.issue(
                "hydration_unconfirmed",
                "No verified target was observed; an empty/incomplete page does not prove login is required.",
            )
            if any(
                k.startswith("policy_") or k in {"request_failed", "resource_http_error", "resource_transport_error"}
                for k in self.diagnostics["counts"]
            ):
                self.run.issue(
                    "acquisition_policy_limited",
                    "Blocked/failed resources were observed; their effect on target acquisition is unconfirmed. See diagnostics.",
                )
        self.run.snapshot.update(self.snapshot)
        return self.snapshot

    async def expand_thread(self) -> dict:
        if not self.snapshot.get("canonical_url"):
            raise Failure("unresolved_target")
        # Bounded, recorded expansion of the loaded conversation, then re-read the observable public data.
        await self.observe()
        expansion = await self._expand_visible_thread()
        self.run.event(
            "thread_expansion",
            input={"canonical_url": self.snapshot.get("canonical_url")},
            output=expansion,
            status="observed",
        )
        doc = await self.page.evaluate(
            """() => ({canonical:document.querySelector('link[rel="canonical"]')?.href ?? null,
          scripts:[...document.querySelectorAll('script[type="application/json"],script[type="application/ld+json"]')]
            .filter(e=>e.textContent.length<=2000000).slice(0,100).map(e=>e.textContent)})"""
        )
        doc["visible_posts"] = await self.page.evaluate(VISIBLE_POSTS_JS, caption_index(doc["scripts"]))
        doc["thread_observed"] = await self.page.locator(THREAD_REGION_SELECTOR).evaluate_all(
            "els=>els.some(e=>e.getClientRects().length>0)"
        )
        doc["has_more"] = await self.page.locator("button").evaluate_all(
            "els=>els.some(e=>e.getClientRects().length && /^(load more replies|show more replies|載入更多回覆|顯示更多回覆)$/i.test((e.innerText||'').trim()))"
        )
        observed = public_snapshot(doc, self.run.source, self.page.url, list(self.guard.chain), utc())
        self.snapshot, merge = merge_public_snapshots(self.snapshot, observed)
        if merge["decision"] == "rejected_target":
            merge["observed_document_canonical_url"] = doc.get("canonical")
            self.run.issue(
                "evidence_target_mismatch",
                "The later observation did not verify the same canonical target; prior evidence was retained.",
            )
        else:
            self.run.captured_at = self.snapshot.get("captured_at")
            if merge["conflicts"]:
                self.run.issue(
                    "source_evidence_conflict",
                    "Conflicting observations were retained; affected posts and descendants are ambiguous.",
                )
        self.run.event(
            "evidence_merge",
            input={"canonical_url": self.snapshot.get("canonical_url")},
            output=merge,
            status=merge["decision"],
        )
        if self.snapshot.get("has_more"):
            self.run.issue("thread_incomplete", "The page offers additional replies that were not loaded.")
        return self.snapshot

    async def _expand_visible_thread(self) -> dict:
        """Up to three rounds: press a visible 'more replies' button, otherwise scroll to the bottom,
        and stop when the page stops growing. Every round is recorded in the trace; what appears is
        evidence only after the same public-data checks, and nothing here proves the thread ended."""
        actions = {"rounds": 0, "clicks": 0, "scrolls": 0, "stopped": None}
        settle = self.settle_ms if self.transport else 800
        for _ in range(3):
            if self.failure:
                actions["stopped"] = "run_stopped"
                break
            try:
                if min(self.run.remaining(), self.guard.remaining()) < 3:
                    actions["stopped"] = "budget"
                    break
            except Failure:
                actions["stopped"] = "budget"
                break
            actions["rounds"] += 1
            try:
                before = await self.page.evaluate("() => document.documentElement.scrollHeight")
                clicked = await self.page.evaluate(
                    """(pattern) => {const re=new RegExp(pattern,'i');
                      const button=[...document.querySelectorAll('button,[role="button"]')].find(e=>e.getClientRects().length && re.test((e.innerText||'').trim()));
                      if(!button) return false; button.click(); return true;}""",
                    MORE_REPLIES_PATTERN,
                )
                if clicked:
                    actions["clicks"] += 1
                else:
                    await self.page.evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight)")
                    actions["scrolls"] += 1
                await self.page.wait_for_timeout(settle)
                after = await self.page.evaluate("() => document.documentElement.scrollHeight")
            except PlaywrightError:
                actions["stopped"] = "page_error"
                break
            if not clicked and after <= before:
                actions["stopped"] = "no_growth"
                break
        return actions

    async def read_profile(self) -> dict:
        if not self.snapshot.get("canonical_url") or not self.snapshot.get("author"):
            raise Failure("unresolved_target")
        author = self.snapshot["author"]
        url = f"https://www.threads.com/@{author}"
        doc = await self.navigate(url, profile=author)
        try:
            profile_canonical = check_url(doc.get("canonical"), profile=author)
        except Failure:
            raise Failure("profile_identity_unconfirmed")
        if not PROFILE.fullmatch(urlsplit(profile_canonical).path):
            raise Failure("profile_identity_unconfirmed")
        # Only exact matching user records are eligible. No nearby/recommended profile substitution.
        found, names = [], []

        def walk(x):
            if isinstance(x, dict):
                if x.get("username") == author:
                    if isinstance(x.get("full_name"), str) and x["full_name"]:
                        names.append(x["full_name"])
                    count = x.get("follower_count")
                    if (isinstance(count, int) and not isinstance(count, bool) and count >= 0) or isinstance(
                        count, str
                    ):
                        found.append(count)
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)

        for raw in doc["scripts"]:
            try:
                walk(json.loads(raw))
            except (ValueError, RecursionError):
                pass
        if found and len(set(found)) == 1:
            self.snapshot["follower_count"] = found[0]
        # The profile page has its own capture time; captured_at stays the post observation.
        self.snapshot["profile_captured_at"] = utc()
        if names and len(set(names)) == 1:
            self.snapshot["display_name"] = names[0]
        return self.snapshot

    async def close(self) -> None:
        try:
            if self.browser:
                await self.browser.close()
        finally:
            if self.pw:
                await self.pw.stop()
