"""Free news ingestion and automatic EA-calendar merge, with explicit source health."""
import html
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from . import ea

GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
FED = ["https://www.federalreserve.gov/feeds/press_monetary.xml", "https://www.federalreserve.gov/feeds/speeches.xml"]


def clean(value, size=1000):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>"," ",str(value))))[:size].strip()


class BackgroundService:
    def __init__(self, store, broker):
        self.store, self.broker = store, broker
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.thread = None

    def current(self, context=None):
        cfg,_ = self.store.active()
        base = dict(context or cfg["context"])
        cached = self.store.get("background_cache", {})
        fresh = bool(cfg["news"]["enabled"] and cached.get("updated_at") and time.time()-cached["updated_at"] <= cfg["context"]["max_age_hours"]*3600)
        base["news"] = (cached.get("news",[]) if fresh else []) + base["news"]
        base["providers"] = cached.get("providers",[])
        base["news_fresh"] = fresh
        base["news_updated_at"] = cached.get("updated_at",0)
        if cfg["news"]["auto_calendar"]:
            try:
                calendar = ea.import_calendar(self.broker,cfg["context"])
                events={(e["title"],e["time_utc"],e["currency"]):e for e in calendar["events"]}
                # Explicit imported/user blackouts take precedence over a same-event feed item.
                events.update({(e["title"],e["time_utc"],e["currency"]):e for e in cfg["context"]["events"]})
                base.update(events=list(events.values()),calendar_fresh=True,calendar_updated_at=calendar["updated_at"])
            except Exception as exc:
                base.update(calendar_fresh=False,calendar_error=type(exc).__name__)
        else:
            base["calendar_fresh"] = bool(base["updated_at"] and 0 <= time.time()-base["updated_at"] <= base["max_age_hours"]*3600)
        if fresh or base.get("calendar_fresh"):
            base["updated_at"] = max(base["updated_at"],cached.get("updated_at",0),base.get("calendar_updated_at",0))
        base["coverage_note"] = "GDELT 新闻索引 + 美联储公开 RSS；标题/摘要不等于全文。经济日历来自已挂载 EA，覆盖依赖 MT5；缺失不代表无事件。"
        now=time.time()
        base["as_of_iso_utc"]=datetime.fromtimestamp(now,timezone.utc).isoformat()
        base["events"]=[{**e,"time_iso_utc":datetime.fromtimestamp(e["time_utc"],timezone.utc).isoformat(),"minutes_until":round((e["time_utc"]-now)/60,1)} for e in base["events"]]
        base["news"]=[{**n,"time_iso_utc":datetime.fromtimestamp(n["time_utc"],timezone.utc).isoformat(),"age_minutes":round((now-n["time_utc"])/60,1)} for n in base["news"]]
        base["calendar_status"]="fresh" if base.get("calendar_fresh") else "missing_or_stale; cannot conclude no upcoming events"
        return base

    def refresh(self, force=False):
        if not self.lock.acquire(blocking=False): return self.current()
        try:
            cfg,_ = self.store.active()
            settings = cfg["news"]
            cached = self.store.get("background_cache",{})
            now = time.time()
            # Even explicit refresh observes a minimum request interval and cached 429 backoff.
            wait = 60 if force else settings["refresh_minutes"]*60
            if now-cached.get("attempted_at",0)<wait or not settings["enabled"]: return self.current()
            news, providers = [],[]
            with httpx.Client(timeout=settings["timeout_seconds"],follow_redirects=False,headers={"User-Agent":"AgentTradeFusion/0.2 public-news-reader"}) as client:
                if settings["provider"]=="gdelt_fed":
                    try:
                        response = client.get(GDELT,params={"query":settings["query"],"mode":"artlist","format":"json","maxrecords":settings["max_records"],"timespan":f"{settings['lookback_hours']}h","sort":"datedesc"})
                        response.raise_for_status()
                        for item in response.json().get("articles",[]):
                            stamp = datetime.strptime(item["seendate"],"%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
                            news.append({"title":clean(item["title"],300),"summary":"","source":clean(item.get("domain","GDELT"),500),"url":item.get("url",""),"time_utc":stamp,"time_kind":"observed","provider":"GDELT"})
                        providers.append({"name":"GDELT","status":"ok","checked_at":now,"count":len(news)})
                    except Exception as exc:
                        status = getattr(getattr(exc,"response",None),"status_code",None)
                        providers.append({"name":"GDELT","status":"unavailable","checked_at":now,"error":f"HTTP {status}" if status else type(exc).__name__})
                for url in FED:
                    try:
                        response = client.get(url); response.raise_for_status()
                        if len(response.content)>2_000_000: raise ValueError("Feed too large")
                        if b"<!ENTITY" in response.content.upper(): raise ValueError("Entity declarations rejected")
                        rows = ET.fromstring(response.content).findall(".//item")
                        count = 0
                        for item in rows[:50]:
                            stamp = parsedate_to_datetime(item.findtext("pubDate","")).timestamp()
                            if not now-30*86400 <= stamp <= now+60: continue
                            news.append({"title":clean(item.findtext("title",""),300),"summary":clean(item.findtext("description",""),1500),"source":"Federal Reserve","url":item.findtext("link",""),"time_utc":stamp,"time_kind":"published","provider":"Fed RSS"})
                            count += 1
                        providers.append({"name":url,"status":"ok","count":count,"checked_at":now})
                    except Exception as exc:
                        providers.append({"name":url,"status":"unavailable","error":type(exc).__name__,"checked_at":now})
            dedup = {n.get("url") or n["title"]:n for n in news}
            good = any(p["status"]=="ok" for p in providers)
            result={"news":sorted(dedup.values(),key=lambda n:n["time_utc"],reverse=True)[:100] if good else cached.get("news",[]),"providers":providers,"updated_at":now if good else cached.get("updated_at",0),"attempted_at":now}
            self.store.put("background_cache",result)
            return self.current()
        finally:
            self.lock.release()

    def start(self):
        self.wake.clear()
        def loop():
            while not self.wake.is_set():
                try: self.refresh()
                except Exception as exc: self.store.audit("background.error",{"type":type(exc).__name__})
                self.wake.wait(60)
        self.thread = threading.Thread(target=loop,daemon=True,name="fusion-news")
        self.thread.start()

    def stop(self):
        self.wake.set()
