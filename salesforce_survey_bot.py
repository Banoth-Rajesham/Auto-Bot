import asyncio
import random
import csv
import os
import sys
import itertools
from datetime import datetime

import httpx

# ==========================================
# CONSTANTS & CONFIG
# ==========================================
BASE_URL = "https://bluestarlimited.my.salesforce-sites.com/Survey?surveyinvitationid={}"
LOG_FILE = "completed.csv"

# Common headers to mimic a real browser
SCAN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


class SurveyBot:
    def __init__(self, start_id, end_id, tickets, speed_delay=(0.5, 1.5),
                 headless=False, manual_mode=False, auto_submit=False,
                 batch_size=50, proxies=None):
        self.start_id = start_id
        self.end_id = end_id
        self.tickets = list(tickets) if manual_mode else set(tickets)
        self.delay_range = speed_delay
        self.headless = headless
        self.stop_requested = False
        self.manual_mode = manual_mode
        self.auto_submit = auto_submit
        self.batch_size = batch_size  # Now controls concurrent HTTP requests (can be 50-200)
        self.proxies = proxies if proxies else []
        self.tickets_normalized = {self._normalize(t) for t in tickets} if not manual_mode else set()
        # Map normalized -> original for display
        self._ticket_display = {}
        if not manual_mode:
            for t in tickets:
                self._ticket_display[self._normalize(t)] = t

        if not os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(["Timestamp", "SurveyID", "TicketNumber", "Status"])

    def _normalize(self, text):
        return text.replace(" ", "").replace("\n", "").replace("\r", "").lower()

    def log(self, message):
        """Format log for UI consumption."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        return f"[{timestamp}] {message}"

    def save_completion(self, ticket, survey_id, status):
        with open(LOG_FILE, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([datetime.now().isoformat(), survey_id, ticket, status])

    # ==========================================================
    # FAST HTTP SCANNING (replaces slow Playwright scanning)
    # ==========================================================
    async def _check_id_http(self, client, semaphore, survey_id):
        """Check a single survey ID using a fast HTTP GET request."""
        if self.stop_requested:
            return None

        async with semaphore:
            url = BASE_URL.format(survey_id)
            try:
                resp = await client.get(url, timeout=15.0, follow_redirects=True)
                if resp.status_code != 200:
                    return None

                content_norm = self._normalize(resp.text)

                # Check if any of our tickets appear in the page
                for t in self.tickets_normalized:
                    if t in content_norm:
                        original = self._ticket_display.get(t, t)
                        return (survey_id, original, url)

            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError):
                return None
            except Exception:
                return None

        return None

    async def _scan_batch_http(self, batch, emit, processed_ids):
        """Scan a batch of IDs concurrently using httpx."""
        # Filter already processed
        ids_to_scan = [sid for sid in batch if str(sid) not in processed_ids]
        if not ids_to_scan:
            return []

        # Setup proxy if available
        proxy_url = None
        if self.proxies:
            proxy_url = random.choice(self.proxies)

        semaphore = asyncio.Semaphore(self.batch_size)

        async with httpx.AsyncClient(
            headers=SCAN_HEADERS,
            proxy=proxy_url,
            verify=False,  # Skip SSL verification for speed
            limits=httpx.Limits(
                max_connections=self.batch_size,
                max_keepalive_connections=self.batch_size
            ),
        ) as client:
            tasks = [self._check_id_http(client, semaphore, sid) for sid in ids_to_scan]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        matches = []
        for res in results:
            if isinstance(res, tuple) and res is not None:
                matches.append(res)

        return matches

    # ==========================================================
    # INTERACTIVE SUBMISSION (keeps Playwright for user interaction)
    # ==========================================================
    async def _handle_manual_submission(self, p, survey_id, ticket_key, url, emit):
        """Launches a VISIBLE browser for the user to complete the found survey."""
        is_cloud = sys.platform != 'win32'

        if not is_cloud:
            # === LOCAL WINDOWS MODE (Interactive Browser) ===
            emit(f"\n\U0001f514 SUCCESS! Launching Browser for Survey {survey_id} -> Ticket: {ticket_key}")

            browser = await p.chromium.launch(headless=False, args=["--start-maximized"])
            context = await browser.new_context(no_viewport=True)
            page = await context.new_page()

            try:
                await page.goto(url, timeout=60000)
                emit(f"\u2705 Ready! Ticket: {ticket_key}")

                # Pre-fill helper
                try:
                    await page.evaluate("""() => {
                        document.querySelectorAll('textarea').forEach(t => {
                            t.value = 'Good work';
                            t.dispatchEvent(new Event('input', {bubbles: true}));
                        });
                    }""")
                except:
                    pass

                emit("\u23f3 Waiting for you to Submit... (I will detect 'Thank You' page)")

                success_detected = False
                while not self.stop_requested:
                    try:
                        if page.is_closed():
                            emit("\u274c Browser closed manually.")
                            self.stop_requested = True
                            break
                        body_text = (await page.inner_text("body")).lower()
                        if "already been recorded" in body_text or "thank you for your submission" in body_text:
                            success_detected = True
                            break
                    except:
                        pass
                    await asyncio.sleep(1)

                if success_detected:
                    emit(f"\U0001f389 Completed {survey_id}!")
                    self.save_completion(ticket_key, survey_id, "Manual Success")

            except Exception as e:
                emit(f"\u274c Error in interactive mode: {e}")
            finally:
                await browser.close()

        else:
            # === CLOUD / MOBILE MODE ===
            emit(f"\U0001f514 MATCH FOUND! Ticket: {ticket_key}")
            emit(f"\U0001f449 **[CLICK HERE TO OPEN SURVEY]({url})**")
            emit("\u23f3 Please fill and submit on your device!")

            # Monitor with httpx (no browser needed)
            try:
                async with httpx.AsyncClient(headers=SCAN_HEADERS, verify=False) as client:
                    while not self.stop_requested:
                        try:
                            resp = await client.get(url, timeout=15.0, follow_redirects=True)
                            body_lower = resp.text.lower()
                            if "already been recorded" in body_lower or "thank you for your submission" in body_lower:
                                emit(f"\U0001f389 Detected Completion for {survey_id}!")
                                self.save_completion(ticket_key, survey_id, "Cloud Manual Success")
                                break
                        except:
                            pass
                        await asyncio.sleep(5)
            except:
                pass

    # ==========================================================
    # MAIN RUN LOGIC
    # ==========================================================
    async def run_async(self, progress_callback=None):
        def emit(msg):
            formatted = self.log(msg)
            if progress_callback:
                progress_callback(formatted)
            print(formatted)

        # Load History
        processed_ids = set()
        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, 'r') as f:
                    reader = csv.reader(f)
                    next(reader, None)
                    for row in reader:
                        if len(row) > 1:
                            processed_ids.add(row[1])
            except:
                pass

        if self.manual_mode:
            await self._run_direct_mode(emit, processed_ids)
        else:
            await self._run_scan_mode(emit, processed_ids)

        emit("\U0001f3c1 Automation Finished.")

    async def _run_direct_mode(self, emit, processed_ids):
        """Direct mode: open each survey ID for manual submission."""
        from playwright.async_api import async_playwright

        emit(f"\U0001f680 Starting Direct Mode for {len(self.tickets)} IDs...")
        is_cloud = sys.platform != 'win32'

        async with async_playwright() as p:
            if not is_cloud:
                browser = await p.chromium.launch(headless=False, args=["--start-maximized"])
            else:
                browser = await p.chromium.launch(headless=True)
                emit("\u26a0\ufe0f Cloud Mode: I will provide links for you to open manually.")

            context = await browser.new_context(no_viewport=True)
            page = await context.new_page()

            for ticket in self.tickets:
                if self.stop_requested:
                    break
                sid = str(ticket).strip()
                if sid in processed_ids:
                    emit(f"\u23ed\ufe0f Skipping {sid}")
                    continue

                emit(f"\U0001f50d Opening {sid}...")
                url = BASE_URL.format(sid)

                if is_cloud:
                    emit(f"\U0001f449 **[CLICK HERE TO OPEN {sid}]({url})**")
                    emit("\u23f3 Waiting for you to submit...")
                    while True:
                        if self.stop_requested:
                            break
                        try:
                            await page.goto(url, timeout=30000)
                            txt = (await page.inner_text("body")).lower()
                            if "thank you" in txt or "already been recorded" in txt:
                                self.save_completion(sid, sid, "Manual Success")
                                emit("\U0001f389 Verified! Next!")
                                break
                        except:
                            pass
                        await asyncio.sleep(5)
                else:
                    try:
                        await page.goto(url)
                        try:
                            txt = (await page.inner_text("body")).lower()
                            if "thank you" in txt or "already been recorded" in txt:
                                emit(f"\u26a0\ufe0f {sid} already completed.")
                                continue
                        except:
                            pass

                        emit("\u23f3 Waiting for Submit...")
                        while True:
                            if self.stop_requested or page.is_closed():
                                break
                            try:
                                txt = (await page.inner_text("body")).lower()
                                if "thank you" in txt or "already been recorded" in txt:
                                    self.save_completion(sid, sid, "Manual Success")
                                    emit("\U0001f389 Next!")
                                    break
                            except:
                                pass
                            await asyncio.sleep(1)
                    except:
                        pass

            await browser.close()

    async def _run_scan_mode(self, emit, processed_ids):
        """FAST scan mode: use httpx to scan IDs, Playwright only for matches."""
        from playwright.async_api import async_playwright

        end_text = self.end_id if self.end_id else "Unlimited"
        total = (self.end_id - self.start_id + 1) if self.end_id else "\u221e"
        emit(f"\U0001f680 TURBO SCAN: IDs {self.start_id} \u2192 {end_text} ({total} IDs) | Concurrency: {self.batch_size}")
        emit(f"\U0001f3af Watching for {len(self.tickets)} ticket(s)")

        if self.end_id:
            id_iter = range(self.start_id, self.end_id + 1)
        else:
            id_iter = itertools.count(self.start_id)

        iterator = iter(id_iter)
        total_scanned = 0
        total_matches = 0
        start_time = datetime.now()

        # Scan in mega-batches using httpx
        while not self.stop_requested:
            # Collect a large batch
            batch = []
            try:
                for _ in range(self.batch_size):
                    sid = next(iterator)
                    batch.append(sid)
            except StopIteration:
                pass

            if not batch:
                break

            batch_start = datetime.now()
            emit(f"\u26a1 Scanning {batch[0]}\u2013{batch[-1]} ({len(batch)} IDs)...")

            matches = await self._scan_batch_http(batch, emit, processed_ids)
            total_scanned += len(batch)

            elapsed = (datetime.now() - batch_start).total_seconds()
            speed = len(batch) / elapsed if elapsed > 0 else 0
            emit(f"\u2705 Batch done in {elapsed:.1f}s ({speed:.0f} IDs/sec) | Scanned: {total_scanned}")

            if matches:
                total_matches += len(matches)
                emit(f"\u2728 Found {len(matches)} match(es)! Opening...")

                # Use Playwright only for the matched surveys
                async with async_playwright() as p:
                    for survey_id, ticket_key, url in matches:
                        if self.stop_requested:
                            break
                        await self._handle_manual_submission(p, survey_id, ticket_key, url, emit)

            # Small delay between batches to avoid rate limiting
            delay = random.uniform(self.delay_range[0], self.delay_range[1])
            if not self.stop_requested and delay > 0:
                await asyncio.sleep(delay)

        total_time = (datetime.now() - start_time).total_seconds()
        emit(f"\U0001f4ca Summary: Scanned {total_scanned} IDs in {total_time:.1f}s | Matches: {total_matches}")

    def run(self, progress_callback=None):
        """Entry point for Synchronous App."""
        asyncio.run(self.run_async(progress_callback))


if __name__ == "__main__":
    tix = ["I26011331368784"]
    bot = SurveyBot(0, 0, tix, headless=False, manual_mode=True)
    bot.run()
