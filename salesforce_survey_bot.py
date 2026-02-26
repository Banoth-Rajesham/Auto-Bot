import asyncio
import random
import csv
import os
import sys
import itertools
import math
from datetime import datetime

import httpx

# ==========================================
# CONSTANTS & CONFIG
# ==========================================
BASE_URL = "https://bluestarlimited.my.salesforce-sites.com/Survey?surveyinvitationid={}"
LOG_FILE = "completed.csv"
RESULTS_FILE = "survey_results.csv"
MAX_RETRIES = 3  # Retry failed IDs for 100% accuracy

# Rotate user agents to avoid detection
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]


def _get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }


class SurveyBot:
    def __init__(self, start_id, end_id, tickets, speed_delay=(0.2, 0.8),
                 headless=False, manual_mode=False, auto_submit=False,
                 batch_size=100, proxies=None):
        self.start_id = start_id
        self.end_id = end_id
        self.tickets = list(tickets) if manual_mode else set(tickets)
        self.delay_range = speed_delay
        self.headless = headless
        self.stop_requested = False
        self.manual_mode = manual_mode
        self.auto_submit = auto_submit
        self.batch_size = batch_size
        self.proxies = proxies if proxies else []
        self.tickets_normalized = {self._normalize(t) for t in tickets} if not manual_mode else set()
        self._ticket_display = {}
        if not manual_mode:
            for t in tickets:
                self._ticket_display[self._normalize(t)] = t

        # All matches found during scan (for export)
        self.all_matches = []
        # Track which tickets have been matched (for auto-stop)
        self.matched_tickets = set()

        # Initialize CSV files
        for filepath, headers in [
            (LOG_FILE, ["Timestamp", "SurveyID", "TicketNumber", "Status"]),
            (RESULTS_FILE, ["Timestamp", "SurveyID", "TicketNumber", "SurveyURL"]),
        ]:
            if not os.path.exists(filepath):
                with open(filepath, 'w', newline='') as f:
                    csv.writer(f).writerow(headers)

    def _normalize(self, text):
        return text.replace(" ", "").replace("\n", "").replace("\r", "").lower()

    def log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        return f"[{timestamp}] {message}"

    def save_completion(self, ticket, survey_id, status):
        with open(LOG_FILE, 'a', newline='') as f:
            csv.writer(f).writerow([datetime.now().isoformat(), survey_id, ticket, status])

    def save_result(self, ticket, survey_id, url):
        """Save matched survey link to results CSV."""
        with open(RESULTS_FILE, 'a', newline='') as f:
            csv.writer(f).writerow([datetime.now().isoformat(), survey_id, ticket, url])
        self.all_matches.append({
            "Timestamp": datetime.now().isoformat(),
            "SurveyID": survey_id,
            "TicketNumber": ticket,
            "SurveyURL": url,
        })
        self.matched_tickets.add(self._normalize(ticket))

    def all_tickets_found(self):
        """Check if every ticket has at least one matching survey link."""
        if not self.tickets_normalized:
            return False
        return self.tickets_normalized.issubset(self.matched_tickets)

    # ==========================================================
    # FAST HTTP SCANNING WITH RETRY
    # ==========================================================
    async def _check_id_http(self, client, semaphore, survey_id):
        """Check a single survey ID with retry for 100% accuracy."""
        if self.stop_requested:
            return None

        async with semaphore:
            url = BASE_URL.format(survey_id)
            for attempt in range(MAX_RETRIES):
                try:
                    resp = await client.get(url, timeout=15.0, follow_redirects=True)
                    if resp.status_code != 200:
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(0.5)
                            continue
                        return ("FAILED", survey_id)  # Mark as failed for retry

                    content_norm = self._normalize(resp.text)

                    for t in self.tickets_normalized:
                        if t in content_norm:
                            original = self._ticket_display.get(t, t)
                            return ("MATCH", survey_id, original, url)

                    return None  # No match, successfully checked

                except (httpx.TimeoutException, httpx.ConnectError,
                        httpx.ReadError, httpx.PoolTimeout):
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    return ("FAILED", survey_id)
                except Exception:
                    if attempt < MAX_RETRIES - 1:
                        continue
                    return ("FAILED", survey_id)

        return None

    async def _scan_chunk_with_proxy(self, chunk_ids, proxy_url, concurrency,
                                     emit, processed_ids, worker_name):
        """Scan a chunk of IDs using one proxy with high concurrency."""
        ids_to_scan = [sid for sid in chunk_ids if str(sid) not in processed_ids]
        if not ids_to_scan:
            return [], []

        semaphore = asyncio.Semaphore(concurrency)
        proxy_label = proxy_url or "Direct"

        async with httpx.AsyncClient(
            headers=_get_headers(),
            proxy=proxy_url,
            verify=False,
            limits=httpx.Limits(
                max_connections=concurrency,
                max_keepalive_connections=concurrency
            ),
        ) as client:
            tasks = [self._check_id_http(client, semaphore, sid) for sid in ids_to_scan]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        matches = []
        failed_ids = []
        for res in results:
            if isinstance(res, Exception):
                continue
            if res is None:
                continue
            if res[0] == "MATCH":
                matches.append((res[1], res[2], res[3]))  # (survey_id, ticket, url)
            elif res[0] == "FAILED":
                failed_ids.append(res[1])

        return matches, failed_ids

    # ==========================================================
    # MULTI-PROXY PARALLEL SCAN ENGINE
    # ==========================================================
    async def _parallel_proxy_scan(self, all_ids, emit, processed_ids):
        """
        Split ID range across multiple proxies for maximum speed.
        Each proxy scans its chunk simultaneously.
        Failed IDs are retried with a different proxy.
        """
        # Build list of connections: [None (direct)] + proxies
        connections = [None]  # Always include direct connection
        connections.extend(self.proxies)
        num_workers = len(connections)

        # Calculate concurrency per worker
        per_worker_conc = max(10, self.batch_size // num_workers)

        # Split IDs into chunks, one per worker
        chunk_size = math.ceil(len(all_ids) / num_workers)
        chunks = []
        for i in range(num_workers):
            start = i * chunk_size
            end = min(start + chunk_size, len(all_ids))
            if start < len(all_ids):
                chunks.append((all_ids[start:end], connections[i]))

        emit(f"\U0001f500 Splitting {len(all_ids)} IDs across {len(chunks)} worker(s) "
             f"({per_worker_conc} concurrent each)")

        # Launch all workers in parallel
        all_matches = []
        all_failed = []

        worker_tasks = []
        for idx, (chunk, proxy) in enumerate(chunks):
            name = f"W{idx+1}"
            worker_tasks.append(
                self._scan_chunk_with_proxy(
                    chunk, proxy, per_worker_conc, emit, processed_ids, name
                )
            )

        results = await asyncio.gather(*worker_tasks, return_exceptions=True)

        for res in results:
            if isinstance(res, Exception):
                continue
            matches, failed = res
            all_matches.extend(matches)
            all_failed.extend(failed)

        # RETRY failed IDs with a different proxy for 100% accuracy
        if all_failed and not self.stop_requested:
            emit(f"\U0001f504 Retrying {len(all_failed)} failed IDs...")
            retry_proxy = random.choice(connections)
            retry_matches, still_failed = await self._scan_chunk_with_proxy(
                all_failed, retry_proxy, per_worker_conc, emit, processed_ids, "RETRY"
            )
            all_matches.extend(retry_matches)

            if still_failed:
                emit(f"\u26a0\ufe0f {len(still_failed)} IDs could not be checked (server down?)")

        return all_matches

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
        """
        ULTRA-FAST scan mode:
        - Multi-proxy parallel split for maximum speed
        - Retry logic for 100% accuracy
        - Saves ALL matches to CSV (no browser opening)
        """
        end_text = self.end_id if self.end_id else "Unlimited"
        total_ids = (self.end_id - self.start_id + 1) if self.end_id else None
        total_label = str(total_ids) if total_ids else "\u221e"
        num_proxies = len(self.proxies)
        num_workers = 1 + num_proxies  # direct + proxies

        emit(f"\U0001f680 ULTRA SCAN: IDs {self.start_id} \u2192 {end_text} ({total_label} IDs)")
        emit(f"\U0001f3af Watching for {len(self.tickets)} ticket(s)")
        emit(f"\U0001f500 Workers: {num_workers} (1 direct + {num_proxies} proxies) | "
             f"Concurrency: {self.batch_size}")
        emit(f"\U0001f504 Retry: {MAX_RETRIES}x per ID for 100% accuracy")
        emit(f"\U0001f4be Results saved to: {RESULTS_FILE}")
        if not self.end_id:
            emit(f"\U0001f6d1 Auto-stop: Will stop when all {len(self.tickets)} ticket(s) are found")

        if self.end_id:
            all_ids_full = list(range(self.start_id, self.end_id + 1))
        else:
            all_ids_full = None  # Will use iterator for unlimited

        total_scanned = 0
        total_matches = 0
        start_time = datetime.now()

        if all_ids_full is not None:
            # FINITE RANGE: process in mega-batches
            mega_batch_size = self.batch_size * max(num_workers, 1)

            for i in range(0, len(all_ids_full), mega_batch_size):
                if self.stop_requested:
                    break
                if self.all_tickets_found():
                    emit(f"\U0001f389 ALL {len(self.tickets)} ticket(s) matched! Stopping early.")
                    break

                mega_batch = all_ids_full[i:i + mega_batch_size]
                batch_start = datetime.now()

                emit(f"\u26a1 Scanning {mega_batch[0]}\u2013{mega_batch[-1]} "
                     f"({len(mega_batch)} IDs)...")

                matches = await self._parallel_proxy_scan(
                    mega_batch, emit, processed_ids
                )
                total_scanned += len(mega_batch)

                elapsed = (datetime.now() - batch_start).total_seconds()
                speed = len(mega_batch) / elapsed if elapsed > 0 else 0

                # Save all matches to CSV
                for survey_id, ticket_key, url in matches:
                    self.save_result(ticket_key, survey_id, url)
                    self.save_completion(ticket_key, survey_id, "Scan Match")
                    processed_ids.add(str(survey_id))

                total_matches += len(matches)
                found = len(self.matched_tickets)
                total_tix = len(self.tickets)
                remaining = total_tix - found

                pct = (total_scanned / len(all_ids_full)) * 100
                eta_sec = (elapsed / len(mega_batch)) * (len(all_ids_full) - total_scanned) if elapsed > 0 else 0
                eta_min = eta_sec / 60

                emit(f"\u2705 {elapsed:.1f}s ({speed:.0f} IDs/sec) | "
                     f"Progress: {total_scanned}/{len(all_ids_full)} ({pct:.1f}%) | "
                     f"Matches: {total_matches} | Tickets: {found}/{total_tix} | ETA: {eta_min:.1f}min")

                if matches:
                    for sid, tkt, url in matches:
                        emit(f"  \u2728 Match: Ticket={tkt} | Survey={sid} | {url}")

                # Tiny delay between mega-batches
                delay = random.uniform(self.delay_range[0], self.delay_range[1])
                if not self.stop_requested and delay > 0:
                    await asyncio.sleep(delay)
        else:
            # UNLIMITED MODE: stream batches
            iterator = itertools.count(self.start_id)
            mega_batch_size = self.batch_size * max(num_workers, 1)

            while not self.stop_requested:
                # Auto-stop when all tickets are found
                if self.all_tickets_found():
                    emit(f"\U0001f389 ALL {len(self.tickets)} ticket(s) matched! Auto-stopping.")
                    break

                mega_batch = []
                try:
                    for _ in range(mega_batch_size):
                        mega_batch.append(next(iterator))
                except StopIteration:
                    pass

                if not mega_batch:
                    break

                batch_start = datetime.now()
                emit(f"\u26a1 Scanning {mega_batch[0]}\u2013{mega_batch[-1]} "
                     f"({len(mega_batch)} IDs)...")

                matches = await self._parallel_proxy_scan(
                    mega_batch, emit, processed_ids
                )
                total_scanned += len(mega_batch)

                elapsed = (datetime.now() - batch_start).total_seconds()
                speed = len(mega_batch) / elapsed if elapsed > 0 else 0

                for survey_id, ticket_key, url in matches:
                    self.save_result(ticket_key, survey_id, url)
                    self.save_completion(ticket_key, survey_id, "Scan Match")
                    processed_ids.add(str(survey_id))

                total_matches += len(matches)
                found = len(self.matched_tickets)
                total_tix = len(self.tickets)
                remaining = total_tix - found

                emit(f"\u2705 {elapsed:.1f}s ({speed:.0f} IDs/sec) | "
                     f"Scanned: {total_scanned} | Matches: {total_matches} | "
                     f"Tickets: {found}/{total_tix} ({remaining} remaining)")

                if matches:
                    for sid, tkt, url in matches:
                        emit(f"  \u2728 Match: Ticket={tkt} | Survey={sid} | {url}")

                delay = random.uniform(self.delay_range[0], self.delay_range[1])
                if not self.stop_requested and delay > 0:
                    await asyncio.sleep(delay)

        total_time = (datetime.now() - start_time).total_seconds()
        emit(f"\n\U0001f4ca FINAL SUMMARY")
        emit(f"   Scanned: {total_scanned} IDs in {total_time:.1f}s "
             f"({total_scanned/total_time:.0f} IDs/sec)" if total_time > 0 else "")
        emit(f"   Matches Found: {total_matches}")
        emit(f"   Results File: {os.path.abspath(RESULTS_FILE)}")

    def run(self, progress_callback=None):
        """Entry point for Synchronous App."""
        asyncio.run(self.run_async(progress_callback))


if __name__ == "__main__":
    tix = ["I26011331368784"]
    bot = SurveyBot(0, 0, tix, headless=False, manual_mode=True)
    bot.run()
