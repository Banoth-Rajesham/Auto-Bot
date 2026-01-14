import asyncio
import random
import csv
import os
import itertools
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ==========================================
# CONSTANTS & CONFIG
# ==========================================
BASE_URL = "https://bluestarlimited.my.salesforce-sites.com/Survey?surveyinvitationid={}"
LOG_FILE = "completed.csv"

class SurveyBot:
    def __init__(self, start_id, end_id, tickets, speed_delay=(2, 5), headless=False, manual_mode=False, auto_submit=False, batch_size=6, proxies=None):
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
        
        if not os.path.exists(LOG_FILE):
             with open(LOG_FILE, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(["Timestamp", "SurveyID/Ticket", "Status"])

    def _normalize(self, text):
        return text.replace(" ", "").replace("\n", "").lower()

    def log(self, message):
        """Format log for UI consumption."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        return f"[{timestamp}] {message}"

    def save_completion(self, ticket, survey_id, status):
        with open(LOG_FILE, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([datetime.now().isoformat(), survey_id, ticket, status])

    async def _check_id_async(self, context, survey_id):
        """Scans a single ID within the headless context with Robust Retry."""
        if self.stop_requested: return None
        
        try:
            page = await context.new_page()
            # Optimize: Block media to save bandwidth, but allow scripts
            await page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "media", "font", "stylesheet"] else route.continue_())
            
            url = BASE_URL.format(survey_id)
            found = None
            
            # 1. Load Page with stricter timeout for mass scanning
            await page.goto(url, timeout=20000, wait_until="domcontentloaded")
            
            # 2. Robust Polling for Content
            max_retries = 3
            for i in range(max_retries):
                if self.stop_requested: break
                
                wait_time = 2000 if i == 0 else 1500
                await page.wait_for_timeout(wait_time)
                
                # Extract text from ALL frames
                all_content = ""
                try:
                    all_content += await page.inner_text("body", timeout=1000)
                except: pass
                
                for frame in page.frames:
                    try:
                        all_content += " " + await frame.inner_text("body", timeout=500)
                    except: pass
                
                content_norm = self._normalize(all_content)
                
                # Check for Match
                for t in self.tickets_normalized:
                    if t in content_norm:
                        found = (survey_id, t)
                        break
                
                if found:
                    break
        except Exception:
            return None
        finally:
            try:
                await page.close()
            except: pass
            
        return found

    async def _handle_manual_submission(self, p, survey_id, ticket_key, emit):
        """Launches a VISIBLE browser (Local) or WATCHER (Cloud) for the user to complete the found survey."""
        url = BASE_URL.format(survey_id)
        import sys
        is_cloud = sys.platform != 'win32'
        
        if not is_cloud:
            # === LOCAL WINDOWS MODE (Interactive Browser) ===
            emit(f"\n🔔 SUCCESS! Launching Interactive Browser for {survey_id}...")
            
            browser = await p.chromium.launch(headless=False, args=["--start-maximized"])
            context = await browser.new_context(no_viewport=True)
            page = await context.new_page()
            
            try:
                await page.goto(url, timeout=60000)
                emit(f"✅ Ready! Ticket: {ticket_key}")
                
                # Pre-fill helper
                try:
                    await page.evaluate("""() => {
                        document.querySelectorAll('textarea').forEach(t => { t.value = 'Good work'; t.dispatchEvent(new Event('input', {bubbles: true})); });
                    }""")
                except: pass

                emit("⏳ Waiting for you to Submit... (I will detect 'Thank You' page)")
                
                success_detected = False
                while not self.stop_requested:
                    try:
                        if page.is_closed():
                            emit("❌ Browser closed manually.")
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
                    emit(f"🎉 Completed {survey_id}!")
                    self.save_completion(ticket_key, survey_id, "Manual Success")

            except Exception as e:
                emit(f"❌ Error in interactive mode: {e}")
            finally:
                await browser.close()
        
        else:
            # === CLOUD / MOBILE MODE (Headless Watcher) ===
            # We cannot launch a visible browser. We allow the user to click the link on their device.
            emit(f"🔔 MATCH FOUND! Ticket: {ticket_key}")
            emit(f"👉 **[CLICK HERE TO OPEN SURVEY]({url})**") 
            emit("⏳ I am monitoring the status in the background. Please fill and submit it on your device!")

            # Use a headless monitor to wait for completion
            monitor_browser = await p.chromium.launch(headless=True)
            page = await monitor_browser.new_page()
            
            success_detected = False
            try:
                while not self.stop_requested:
                    try:
                        # Reload page to check status
                        await page.goto(url, timeout=30000, wait_until="domcontentloaded")
                        body_text = (await page.inner_text("body")).lower()
                        
                        if "already been recorded" in body_text or "thank you for your submission" in body_text:
                            success_detected = True
                            break
                        
                        # Wait before checking again
                        await asyncio.sleep(5)
                    except:
                        await asyncio.sleep(5)
                
                if success_detected:
                    emit(f"🎉 Detected Completion for {survey_id}!")
                    self.save_completion(ticket_key, survey_id, "Cloud Manual Success")
            finally:
                await monitor_browser.close()

    async def run_async(self, progress_callback):
        def emit(msg):
            formatted = self.log(msg)
            if progress_callback: progress_callback(formatted)
            print(formatted)

        # Load History
        processed_ids = set()
        if os.path.exists(LOG_FILE):
             try:
                with open(LOG_FILE, 'r') as f:
                    reader = csv.reader(f)
                    next(reader, None)
                    for row in reader:
                        if len(row) > 1: processed_ids.add(row[1])
             except: pass

        async with async_playwright() as p:
            if self.manual_mode:
                # === DIRECT MODE ===
                emit(f"🚀 Starting Direct Mode for {len(self.tickets)} IDs...")
                
                # Check Environment
                import sys
                is_cloud = sys.platform != 'win32'

                if not is_cloud:
                    browser = await p.chromium.launch(headless=False, args=["--start-maximized"])
                else:
                    # Cloud mode: use headless for scanning, no visual browser
                    browser = await p.chromium.launch(headless=True)
                    emit("⚠️ Cloud Mode Detected: I will provide links for you to open manually.")

                context = await browser.new_context(no_viewport=True)
                page = await context.new_page()

                for ticket in self.tickets:
                    if self.stop_requested: break
                    sid = str(ticket).strip()
                    if sid in processed_ids: 
                        emit(f"⏭️ Skipping {sid}")
                        continue
                    
                    emit(f"🔍 Checking {sid}...")
                    url = BASE_URL.format(sid)
                    
                    if is_cloud:
                        # Just provide link and wait
                         emit(f"👉 **[CLICK HERE TO OPEN {sid}]({url})**")
                         emit("⏳ Waiting for you to submit on your device...")
                         # Loop check
                         while True:
                            if self.stop_requested: break
                            try:
                                await page.goto(url, timeout=30000)
                                txt = (await page.inner_text("body")).lower()
                                if "thank you" in txt or "already been recorded" in txt:
                                    self.save_completion(sid, sid, "Manual Success")
                                    emit("🎉 Verified! Next!")
                                    break
                            except: pass
                            await asyncio.sleep(5)
                    else:
                        # Desktop Interactive
                        try:
                            await page.goto(url)
                            try:
                                txt = (await page.inner_text("body")).lower()
                                if "thank you" in txt or "already been recorded" in txt:
                                    emit(f"⚠️ {sid} already completed.")
                                    continue
                            except: pass

                            emit("⏳ Waiting for Submit...")
                            while True:
                                if self.stop_requested or page.is_closed(): break
                                try:
                                    txt = (await page.inner_text("body")).lower()
                                    if "thank you" in txt or "already been recorded" in txt:
                                        self.save_completion(sid, sid, "Manual Success")
                                        emit("🎉 Next!")
                                        break
                                except: pass
                                await asyncio.sleep(1)
                        except: pass
                await browser.close()
            
            else:
                # === SCAN MODE ===
                end_text = self.end_id if self.end_id else "Unlimited"
                emit(f"🚀 High-Speed Scan with Anti-Ban Logic {self.start_id}-{end_text}")

                if self.end_id:
                    id_iter = range(self.start_id, self.end_id + 1)
                else:
                    id_iter = itertools.count(self.start_id)
                
                iterator = iter(id_iter)
                
                scanner_browser = await p.chromium.launch(headless=True)
                
                # Context managed inside loop for Proxy Rotation
                while not self.stop_requested:
                    batch = []
                    try:
                        for _ in range(self.batch_size): 
                            sid = next(iterator)
                            if str(sid) not in processed_ids:
                                batch.append(sid)
                    except StopIteration:
                        pass
                    
                    if not batch: break

                    # PROXY ROTATION
                    context_options = {}
                    if self.proxies:
                        curr = random.choice(self.proxies)
                        context_options["proxy"] = {"server": curr}
                    
                    scanner_context = await scanner_browser.new_context(**context_options)

                    emit(f"⚡ Scanning batch {batch[0]}-{batch[-1]} (C={self.batch_size})...")

                    try:
                        tasks = []
                        for sid in batch:
                            tasks.append(self._check_id_async(scanner_context, sid))
                            await asyncio.sleep(0.02) # Faster stagger
                        
                        results = await asyncio.gather(*tasks)
                        
                        matches = []
                        for res in results:
                            if res: matches.append(res)
                        
                        matches.sort(key=lambda x: int(x[0])) # Process sequentially

                        if matches:
                            emit(f"✨ Found {len(matches)} matches! Processing...")
                            for survey_id, ticket_key in matches:
                                if self.stop_requested: break
                                await self._handle_manual_submission(p, survey_id, ticket_key, emit)
                    finally:
                        await scanner_context.close() # Rotate IP

                await scanner_browser.close()
        
        emit("🏁 Automation Finished.")

    def run(self, progress_callback=None):
        """Entry point for Synchronous App."""
        asyncio.run(self.run_async(progress_callback))

if __name__ == "__main__":
    tix = ["I26011331368784"] 
    bot = SurveyBot(0, 0, tix, headless=False, manual_mode=True)
    bot.run()
