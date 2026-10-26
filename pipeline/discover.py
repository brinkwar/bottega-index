#!/usr/bin/env python3
"""
BOTTEGA_DISCOVER_V15

discover.py — find each institution's course catalog and fingerprint the
platform it runs on.

Reads roster.json. For every institution it tries a short list of conventional
catalog URLs, falls back to scanning the homepage for a catalog link, and then
identifies which commercial catalog platform the page runs on. The output is a
platform census, and that census is what tells us how much adapter work
harvest.py actually needs.

WHAT THIS MEASURES: where a catalog lives and what software serves it. Nothing
about curriculum is read here.

CONDUCT
  robots.txt honored. One request per second per domain. Named user agent with
  a contact URL. Every fetch cached to disk and never repeated. Resumable.

USAGE
  python3 discover.py --roster data/roster.json --limit 20        # start here
  python3 discover.py --roster data/roster.json                   # ranking tier
  python3 discover.py --roster data/roster.json --all             # full census

OUTPUTS
  data/catalogs.json      per-institution result
  data/catalogs.csv       same, for a spreadsheet
  data/discover_run.log   audit trail + the platform census
  cache/discover/         raw pages, keyed by URL hash. Reused by harvest.py
"""

import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections import Counter, defaultdict

try:
    import browser as B
except ImportError:      # browser.py absent; challenged hosts stay unharvestable
    B = None
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

MARKER = "BOTTEGA_DISCOVER_V15"
DEFAULT_CONTACT = "https://artmorph.org/bottega-index/method; hello@artmorph.org"
UA_TMPL = ("Mozilla/5.0 (compatible; BottegaIndexBot/1.0; "
           "Art Morph Institute; +{contact})")

# ---------------------------------------------------------------- platforms

# Ordered. First platform with any marker hit wins; more hits raise confidence.
PLATFORM_MARKERS = [
    ("courseleaf", [
        "courseleaf", "leepfrog", "/ribbit/", "sc_courselist",
        "sc_sccoursedescs", "sc_courseinventory", "/coursesaz/",
    ]),
    ("acalog", [
        "acalog", "preview_program.php", "preview_course.php",
        "content.php?catoid", "digarc", "ax_link", "moderncampus",
    ]),
    ("coursedog", ["coursedog"]),
    ("kuali", ["kuali.co", "kuali catalog", "kualico"]),
    ("smartcatalog", ["smartcatalogiq", "smartcatalog", "watermarkinsights"]),
    ("cleancatalog", ["cleancatalog", "clean catalog"]),
    ("ellucian", ["ellucian"]),
]

# Host-level giveaways beat page-body markers.
HOST_MARKERS = {
    "coursedog.com": "coursedog",
    "kuali.co": "kuali",
    "smartcatalogiq.com": "smartcatalog",
    "cleancatalog.io": "cleancatalog",
    "acalogadmin.com": "acalog",
}

# Conventional catalog subdomains and paths, best guesses first.
SUBDOMAINS = ["catalog", "catalogue", "bulletin", "bulletins", "catalogs",
              "courses", "academiccatalog", "coursecatalog", "academics",
              "registrar", "programs", "coursecatalogue"]
PATHS = ["/catalog", "/catalog/", "/catalogue", "/bulletin", "/bulletins",
         "/academics/catalog", "/academics/catalogs", "/academics/course-catalog",
         "/registrar/catalog", "/registrar/catalogs", "/provost/catalog",
         "/academic-catalog", "/academic-catalogs", "/course-catalog",
         "/registrar/academic-catalog", "/catalog/index.html",
         "/course-catalog.html", "/academics/academic-catalog"]

# URLs that smell like a marketing or admissions page. Not rejected outright,
# because some schools genuinely park the catalog behind one of these paths.
# Instead they are demoted, which forces the one-hop refine below.
MARKETING_URL = re.compile(
    r"(get-info|request-info|/apply|/visit|/admission|/inquiry|/rfi\b)", re.I)

TRACKING_PARAM = re.compile(r"^(utm_|fbclid|gclid|mc_|_ga)", re.I)

# Repeated course codes are the strongest evidence a page is really a catalog.
COURSE_CODE = re.compile(r"\b[A-Z]{2,5}[\s\-]?\d{3,4}[A-Z]?\b")
PROGRAM_LINK = re.compile(r"(program|degree|major|minor|course|curriculum)", re.I)

# A page that is plausibly a catalog rather than a marketing page.
CATALOG_SIGNAL = re.compile(
    r"(course\s+catalog|academic\s+catalog|catalog\s+home|undergraduate\s+catalog|"
    r"graduate\s+catalog|academic\s+bulletin|courses\s+of\s+instruction|"
    r"programs\s+of\s+study|course\s+descriptions)", re.I)

LINK_RE = re.compile(rb'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.{0,120}?)</a>',
                     re.I | re.S)

# Catalogs are versioned by academic year. CourseLeaf and Acalog both serve
# archives at stable URLs, so a stale link returns an old edition without ever
# erroring. Detect the edition and record it, or the study silently dates itself.
YEAR_RANGE = re.compile(r"\b(20\d{2})\s*[-\u2013\u2014/]\s*(20\d{2}|\d{2})\b")
TITLE_RE = re.compile(r"<title[^>]*>(.{0,300}?)</title>", re.I | re.S)
H1_RE = re.compile(r"<h1[^>]*>(.{0,300}?)</h1>", re.I | re.S)
# CourseLeaf and Acalog roots often carry the edition only in a catalog-picker
# dropdown rather than the title.
OPTION_RE = re.compile(r"<option[^>]*>(.{0,120}?)</option>", re.I | re.S)

# A host or path that exists for no purpose other than serving the catalog.
# Location alone is strong evidence, which matters because client-rendered
# catalogs ship almost no HTML for a structure test to read.
CATALOG_HOST = re.compile(
    r"^(catalog|catalogue|bulletin|bulletins|catalogs|courses|"
    r"academiccatalog|coursecatalog)\.", re.I)
CATALOG_PATH = re.compile(
    r"^/(catalog|catalogue|bulletin|bulletins|academic-catalog|"
    r"academic-catalogs|course-catalog)s?/?$", re.I)

# Single-page-app fingerprints. These catalogs build themselves in the browser,
# so harvest.py has to talk to the JSON API behind them, not scrape HTML.
SPA_MARKERS = ["__next_data__", "ng-app", "ng-version", "data-reactroot",
               "react-dom", "vue.js", "__nuxt__", "window.__initial",
               'id="root"', "id='root'", 'id="app"', "id='app'"]

LOG = []
LOG_LOCK = threading.Lock()
DOMAIN_LOCKS = defaultdict(threading.Lock)
LAST_HIT = {}
LAST_HIT_LOCK = threading.Lock()
ROBOTS = {}
ROBOTS_LOCK = threading.Lock()
CURRENT_EDITION = ""


def log(msg):
    with LOG_LOCK:
        LOG.append(str(msg))
        print(msg, file=sys.stderr)


def hst_today():
    return datetime.now(timezone(timedelta(hours=-10))).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- fetching

class Fetcher:
    def __init__(self, cache_dir, contact, delay, timeout, insecure=False,
                 refresh=False, solve_challenges=True):
        self.refresh = refresh
        self.cache_dir = cache_dir
        self.solve_challenges = solve_challenges
        # Per-host WAF tokens, loaded from disk so a rerun does not re-solve.
        self.tokens = B.load_tokens(cache_dir) if B else {}
        self._token_lock = threading.Lock()
        self._token_failed = set()
        self.ua = UA_TMPL.format(contact=contact)
        self.delay = delay
        self.timeout = timeout
        os.makedirs(cache_dir, exist_ok=True)
        if insecure:
            import ssl
            self.ctx = ssl._create_unverified_context()
        else:
            self.ctx = None

    def _cache_path(self, url):
        h = hashlib.sha256(url.encode()).hexdigest()[:24]
        return os.path.join(self.cache_dir, h + ".json.gz")

    def _throttle(self, host):
        with LAST_HIT_LOCK:
            last = LAST_HIT.get(host, 0.0)
            wait = self.delay - (time.time() - last)
            LAST_HIT[host] = time.time() + max(wait, 0)
        if wait > 0:
            time.sleep(wait)

    # 202 means accepted-but-not-ready, 429 means slow down, 503 means try later.
    # All three are instructions to wait, not failures, and a crawler that treats
    # them as failures silently loses whole platforms.
    RETRY_STATUS = (202, 429, 503)

    @classmethod
    def cacheable(cls, status):
        """V15. Through V14 everything outside RETRY_STATUS was cached for good,
        including 502s and status 0 (a timeout or dropped connection). One bad
        second during a crawl became a permanent hole on every cache replay:
        SCAD's /courses/adve/ was a cached 502, zero bytes, fifteen courses."""
        return not (status in cls.RETRY_STATUS or status in (0, 403, 408, 425)
                    or 500 <= (status or 0) <= 599)

    # Statuses that mean "prove you can run JavaScript". A fresh challenge often
    # arrives with an EMPTY body — the 2kb challenge page only shows up on some
    # paths — so the status has to be the trigger, not the body content.
    CHALLENGE_STATUS = (202, 403)

    def get(self, url, use_cache=True):
        """Return {'url','status','body','ctype','error'}. Never raises.

        Retries the statuses that mean 'wait', with backoff, and never caches
        one — caching a hold page would poison every later run.
        """
        cp = self._cache_path(url)
        if use_cache and not self.refresh and os.path.exists(cp):
            # A torn cache page is a miss, not a crash. Two concurrent crawls
            # once wrote the same pages; "never raises" has to include this.
            # V15: so is a cached transient failure written before V15 — it is
            # fetched again, never replayed.
            try:
                with gzip.open(cp, "rt", encoding="utf-8") as fh:
                    hit = json.load(fh)
                if self.cacheable(hit.get("status")):
                    return hit
            except (OSError, EOFError, ValueError):
                pass

        out = None
        solves = 0
        for attempt in range(4):
            out = self._fetch_once(url)
            challenged = (out["status"] in self.CHALLENGE_STATUS
                          or B and B.is_challenge(out.get("body")))

            if challenged and B and self.solve_challenges and solves < 2:
                # A JavaScript challenge never clears by waiting. Solve once with
                # whatever token exists, and if that still fails the token is
                # stale, so force a fresh solve before giving up.
                host = urllib.parse.urlparse(url).netloc
                if solves == 0 and not self.tokens.get(host):
                    print(f"  challenge (HTTP {out['status']}) at {host} — solving",
                          file=sys.stderr)
                if self._ensure_token(url, force=(solves == 1)):
                    solves += 1
                    continue
                break

            if out["status"] not in self.RETRY_STATUS:
                break
            time.sleep(2.0 * (attempt + 1))

        if self.cacheable(out["status"]):
            tmp = f"{cp}.{os.getpid()}.{threading.get_ident()}.tmp"
            with gzip.open(tmp, "wt", encoding="utf-8") as fh:
                json.dump(out, fh)
            os.replace(tmp, cp)
        return out

    def _ensure_token(self, url, force=False):
        """Obtain a WAF token for this host. Thread-safe.

        WAF tokens expire. A cached one that still draws a challenge is stale,
        not valid, and force=True drops it and solves again — otherwise a token
        saved by an earlier run silently blocks the host forever.
        """
        host = urllib.parse.urlparse(url).netloc
        with self._token_lock:
            if force:
                if self.tokens.pop(host, None):
                    print(f"  token for {host} was stale; re-solving",
                          file=sys.stderr)
                self._token_failed.discard(host)
            if self.tokens.get(host):
                return True
            if host in self._token_failed:
                return False
            jar = B.solve(url, self.ua, log=lambda m: print(m, file=sys.stderr))
            if jar:
                self.tokens[host] = jar
                B.save_tokens(self.cache_dir, self.tokens)
                print(f"  solved challenge for {host}", file=sys.stderr)
                return True
            print(f"  could not solve challenge for {host}; it will be reported "
                  f"unharvestable", file=sys.stderr)
            self._token_failed.add(host)
            return False

    def _fetch_once(self, url):
        host = urllib.parse.urlparse(url).netloc
        with DOMAIN_LOCKS[host]:
            self._throttle(host)
            out = {"url": url, "status": 0, "body": "", "ctype": "", "error": ""}
            try:
                headers = {
                    "User-Agent": self.ua,
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    "Accept-Encoding": "gzip",
                }
                tok = self.tokens.get(host)
                if tok:
                    headers["Cookie"] = tok
                req = urllib.request.Request(url, headers=headers)
                kw = {"timeout": self.timeout}
                if self.ctx:
                    kw["context"] = self.ctx
                with urllib.request.urlopen(req, **kw) as r:
                    raw = r.read(2_000_000)
                    if r.headers.get("Content-Encoding") == "gzip":
                        try:
                            raw = gzip.decompress(raw)
                        except OSError:
                            pass
                    out["status"] = r.status
                    out["ctype"] = r.headers.get("Content-Type", "")
                    out["url"] = strip_tracking(r.url)
                    if "pdf" not in out["ctype"].lower():
                        for enc in ("utf-8", "cp1252", "latin-1"):
                            try:
                                out["body"] = raw.decode(enc)
                                break
                            except UnicodeDecodeError:
                                continue
            except urllib.error.HTTPError as e:
                out["status"] = e.code
                out["error"] = f"HTTP {e.code}"
            except Exception as e:
                out["error"] = f"{type(e).__name__}: {e}"[:180]
        if out["status"] in self.RETRY_STATUS and not out["error"]:
            out["error"] = f"HTTP {out['status']} (hold page)"
        return out

    def allowed(self, url):
        """robots.txt check, cached per host."""
        p = urllib.parse.urlparse(url)
        key = f"{p.scheme}://{p.netloc}"
        with ROBOTS_LOCK:
            rp = ROBOTS.get(key)
        if rp is None:
            r = self.get(key + "/robots.txt")
            rp = urllib.robotparser.RobotFileParser()
            if r["status"] == 200 and r["body"]:
                try:
                    rp.parse(r["body"].splitlines())
                except Exception:
                    rp.allow_all = True
            else:
                rp.allow_all = True   # no robots.txt means no restriction
            with ROBOTS_LOCK:
                ROBOTS[key] = rp
        try:
            return rp.can_fetch(self.ua, url)
        except Exception:
            return True


# ---------------------------------------------------------------- logic

def base_domain(homepage):
    """'https://www.risd.edu/academics' -> ('https', 'www.risd.edu', 'risd.edu')"""
    p = urllib.parse.urlparse(homepage if "://" in homepage else "https://" + homepage)
    host = p.netloc.lower()
    scheme = p.scheme or "https"
    bare = host[4:] if host.startswith("www.") else host
    return scheme, host, bare


def strip_tracking(url):
    """Drop utm_* and friends. A tracking-tagged URL is a link off a marketing
    page, and it poisons the cache key for no benefit."""
    p = urllib.parse.urlparse(url)
    if not p.query:
        return url
    keep = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
            if not TRACKING_PARAM.match(k)]
    return urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(keep)))


def candidates(homepage):
    """Conventional catalog URLs to try, best guesses first."""
    scheme, host, bare = base_domain(homepage)
    urls = [f"https://{sub}.{bare}/" for sub in SUBDOMAINS]
    urls += [f"{scheme}://{host}{path}" for path in PATHS]
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def canonical_location(url):
    p = urllib.parse.urlparse(url)
    return bool(CATALOG_HOST.match(p.netloc)) or bool(CATALOG_PATH.match(p.path))


def render_mode(res):
    """'client' when the page is an app shell, 'server' otherwise."""
    body = res.get("body", "") or ""
    low = body[:120000].lower()
    hits = [m for m in SPA_MARKERS if m in low]
    n_links = len(LINK_RE.findall(body.encode("utf-8", "ignore")))
    if hits and n_links < 40:
        return "client", hits[:3]
    return "server", []


def catalog_strength(res):
    """'strong' | 'weak' | 'no'.

    strong  a known platform, or catalog phrasing backed by real catalog
            structure (repeated course codes or many program links)
    weak    says catalog but shows no structure, or sits on a marketing URL.
            Accepted only after a one-hop refine fails to find better
    no      not a catalog
    """
    if res["status"] != 200:
        return "no"
    if "pdf" in res["ctype"].lower():
        return "strong"
    body = res["body"]
    if len(body) < 400:
        return "no"

    low = (res["url"] + body[:20000]).lower()
    has_platform = any(m in low for _, ms in PLATFORM_MARKERS for m in ms)
    has_phrase = bool(CATALOG_SIGNAL.search(body))
    marketing = bool(MARKETING_URL.search(res["url"]))

    sample = body[:200000]
    codes = len(set(COURSE_CODE.findall(sample)))
    # Distinct hrefs, not raw matches: a site-wide nav trivially contains a
    # dozen links saying "programs" without being a catalog.
    prog_links = len({h for h, t in LINK_RE.findall(sample.encode("utf-8", "ignore"))
                      if PROGRAM_LINK.search((h + t).decode("utf-8", "ignore"))})
    structured = codes >= 3 or prog_links >= 20

    if marketing:
        # An admissions or get-info page saying "catalog" is a signpost, not a
        # catalog. Nav links do not rescue it; only course codes or a platform do.
        return "weak" if (has_platform or codes >= 3) else "no"
    if has_platform:
        return "strong"
    if canonical_location(res["url"]) and (has_phrase or structured
                                           or render_mode(res)[0] == "client"):
        # catalog.school.edu exists to serve the catalog. A thin body there means
        # client-side rendering, not a missing catalog.
        return "strong"
    if has_phrase and structured:
        return "strong"
    if has_platform or has_phrase:
        return "weak"
    return "no"


def looks_like_catalog(res):
    return catalog_strength(res) != "no"


def detect_catalog_year(res):
    """-> ('2026-2027', 'title'|'url'|'body') or ('', '').

    Only year *ranges* count. A bare '2026' is usually a copyright line.
    """
    body = res.get("body", "") or ""
    url = res.get("url", "") or ""

    def norm(m):
        """Return 'YYYY-YYYY' only if the pair is a real academic year, meaning
        the second year is the first plus one. Everything else in a page that
        looks like a year range is a phone number, an ID, or two unrelated
        numbers that became adjacent when tags were stripped."""
        a, b = m.group(1), m.group(2)
        if len(b) == 2:
            b = a[:2] + b
        try:
            if int(b) - int(a) != 1:
                return None
        except ValueError:
            return None
        return f"{a}-{b}"

    for label, hay in (
        ("title", " ".join(TITLE_RE.findall(body)[:1] + H1_RE.findall(body)[:1])),
        ("url", url),
        ("picker", " ".join(OPTION_RE.findall(body)[:60])),
        ("body", body),
    ):
        hits = [y for y in (norm(m) for m in
                            YEAR_RANGE.finditer(re.sub(r"<[^>]+>", " ", hay))) if y]
        if hits:
            # A picker lists several editions. Prefer the current one; otherwise
            # take the newest that is not implausibly far in the future.
            if CURRENT_EDITION and CURRENT_EDITION in hits:
                return CURRENT_EDITION, label
            try:
                cap = int(CURRENT_EDITION[:4]) + 1 if CURRENT_EDITION else 9999
            except ValueError:
                cap = 9999
            plausible = [h for h in hits if int(h[:4]) <= cap]
            if plausible:
                return max(plausible), label
            # Every hit here is implausibly far in the future, which means the
            # match is noise. Fall through to the next source rather than
            # reporting a fabricated edition.
    return "", ""


def fingerprint(res):
    """-> (platform, confidence, [evidence])"""
    url = res.get("url", "")
    host = urllib.parse.urlparse(url).netloc.lower()
    for frag, plat in HOST_MARKERS.items():
        if frag in host:
            return plat, "high", [f"host contains {frag}"]

    if "pdf" in res.get("ctype", "").lower() or url.lower().endswith(".pdf"):
        return "pdf", "high", ["content-type is PDF"]

    hay = (url + " " + res.get("body", "")[:200000]).lower()
    best = None
    for plat, markers in PLATFORM_MARKERS:
        hits = [m for m in markers if m in hay]
        if hits and (best is None or len(hits) > len(best[2])):
            best = (plat, "high" if len(hits) >= 2 else "medium", hits)
    if best:
        return best
    if res.get("status") == 200 and res.get("body"):
        return "other", "low", ["page reachable, no known platform markers"]
    return "unknown", "none", []


# Hub pages that carry the catalog link when the homepage does not.
HUB_PATHS = ["/academics", "/registrar", "/academics/registrar", "/students",
             "/current-students", "/about/academics", "/academics/index.html",
             "/offices/registrar", "/academics/academic-resources"]


def catalog_links(res, limit=5):
    """Ranked catalog-ish links on a page. Returns absolute URLs, best first."""
    body = (res.get("body") or "").encode("utf-8", "ignore")
    scored = []
    for href, text in LINK_RE.findall(body):
        try:
            h = href.decode("utf-8", "ignore")
            t = re.sub(r"<[^>]+>", " ", text.decode("utf-8", "ignore")).strip().lower()
        except Exception:
            continue
        blob = (h + " " + t).lower()
        if blob.startswith("#") or "javascript:" in blob or "mailto:" in blob:
            continue
        score = 0
        if "catalog" in blob or "catalogue" in blob:
            score += 3
        if "bulletin" in blob:
            score += 3
        if "course" in blob:
            score += 1
        if "curriculum" in blob:
            score += 1
        if "academic" in blob:
            score += 1
        if score:
            scored.append((score, strip_tracking(urllib.parse.urljoin(res["url"], h))))
    scored.sort(key=lambda x: -x[0])
    out, seen = [], set()
    for _, u in scored:
        if u in seen or u.rstrip("/") == res.get("url", "").rstrip("/"):
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= limit:
            break
    return out


def deep_scan(fetcher, home, home_res):
    """When conventions miss, follow the site's own signposting.

    Universities without a single catalog URL almost always link to whatever
    they do have from the homepage or from an academics or registrar hub. Try
    the homepage's own links first, then those hubs.
    """
    for url in catalog_links(home_res, limit=5):
        if not fetcher.allowed(url):
            continue
        r = fetcher.get(url)
        if catalog_strength(r) == "strong":
            return r, "homepage_deep"

    scheme, host, _ = base_domain(home)
    for hub in HUB_PATHS:
        hub_url = f"{scheme}://{host}{hub}"
        if not fetcher.allowed(hub_url):
            continue
        hr = fetcher.get(hub_url)
        if hr["status"] != 200 or not hr["body"]:
            continue
        for url in catalog_links(hr, limit=3):
            if not fetcher.allowed(url):
                continue
            r = fetcher.get(url)
            if catalog_strength(r) == "strong":
                return r, "hub_deep"
    return None, ""


def homepage_catalog_link(fetcher, homepage):
    """Fallback: scan the homepage for something that reads like a catalog link."""
    res = fetcher.get(homepage)
    if res["status"] != 200 or not res["body"]:
        return None, res
    body = res["body"].encode("utf-8", "ignore")
    scored = []
    for href, text in LINK_RE.findall(body):
        try:
            h = href.decode("utf-8", "ignore")
            t = re.sub(r"<[^>]+>", " ", text.decode("utf-8", "ignore")).strip().lower()
        except Exception:
            continue
        blob = (h + " " + t).lower()
        score = 0
        if "catalog" in blob or "catalogue" in blob:
            score += 3
        if "bulletin" in blob:
            score += 2
        if "course" in blob:
            score += 1
        if "curriculum" in blob:
            score += 1
        if score:
            scored.append((score, urllib.parse.urljoin(res["url"], h)))
    if not scored:
        return None, res
    scored.sort(key=lambda s: -s[0])
    return scored[0][1], res


def refine(fetcher, res):
    """Follow the best catalog link one hop. Landing pages that merely point at
    the catalog are common; this is what turns them into real hits."""
    body = res.get("body", "")
    if not body:
        return None
    scored = []
    for href, text in LINK_RE.findall(body.encode("utf-8", "ignore")):
        try:
            h = href.decode("utf-8", "ignore")
            t = re.sub(r"<[^>]+>", " ", text.decode("utf-8", "ignore")).strip().lower()
        except Exception:
            continue
        blob = (h + " " + t).lower()
        if "javascript:" in blob or blob.startswith("#"):
            continue
        score = 0
        if "catalog" in blob or "catalogue" in blob:
            score += 3
        if "bulletin" in blob:
            score += 3
        if "undergraduate" in blob or "graduate" in blob:
            score += 1
        if "course" in blob:
            score += 1
        if score >= 3:
            scored.append((score, strip_tracking(urllib.parse.urljoin(res["url"], h))))
    if not scored:
        return None
    scored.sort(key=lambda s: -s[0])
    seen = set()
    for _, url in scored[:4]:
        if url in seen or url.rstrip("/") == res["url"].rstrip("/"):
            continue
        seen.add(url)
        if not fetcher.allowed(url):
            continue
        r2 = fetcher.get(url)
        if catalog_strength(r2) == "strong":
            return r2
    return None


def load_overrides(path):
    """unitid -> catalog url. The honest way to close the last few percent:
    a human found it, and the file records who decided what."""
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    return {str(k): v for k, v in d.items()}


def discover_one(fetcher, inst, overrides=None):
    out = {
        "unitid": inst["unitid"],
        "name": inst["name"],
        "control": inst.get("control", ""),
        "state": inst.get("state", ""),
        "completions": inst["completions"]["total"],
        "in_ranking_tier": inst.get("in_ranking_tier"),
        "homepage": inst.get("webaddr", ""),
        "catalog_url": "",
        "found_via": "none",
        "platform": "unknown",
        "confidence": "none",
        "evidence": [],
        "status": 0,
        "robots_blocked": False,
        "refined_from": "",
        "catalog_year": "",
        "catalog_year_from": "",
        "render": "",
        "render_evidence": [],
        "error": "",
    }
    def accept_url(res, via, refined_from=""):
        out["catalog_url"] = res["url"]
        out["found_via"] = via
        out["status"] = res["status"]
        out["refined_from"] = refined_from
        out["platform"], out["confidence"], out["evidence"] = fingerprint(res)
        out["catalog_year"], out["catalog_year_from"] = detect_catalog_year(res)
        out["render"], out["render_evidence"] = render_mode(res)
        return out

    ov = (overrides or {}).get(str(inst["unitid"]))
    if ov:
        res = fetcher.get(strip_tracking(ov))
        return accept_url(res, "override")

    home = inst.get("webaddr", "")
    if not home:
        out["error"] = "no homepage in roster"
        return out

    def accept(res, via, refined_from=""):
        out["catalog_url"] = res["url"]
        out["found_via"] = via
        out["status"] = res["status"]
        out["refined_from"] = refined_from
        out["platform"], out["confidence"], out["evidence"] = fingerprint(res)
        out["catalog_year"], out["catalog_year_from"] = detect_catalog_year(res)
        out["render"], out["render_evidence"] = render_mode(res)
        return out

    blocked = 0
    weak = None          # best weak candidate, kept as a last resort
    strong_other = None  # strong, but on software we do not recognize

    for url in candidates(home):
        if not fetcher.allowed(url):
            blocked += 1
            continue
        res = fetcher.get(url)
        grade = catalog_strength(res)
        if grade == "strong":
            plat = fingerprint(res)[0]
            if plat not in ("other", "unknown"):
                # A recognized platform is the best possible answer. Stop here.
                return accept(res, "convention")
            if strong_other is None:
                strong_other = res
            continue
        if grade == "weak" and weak is None:
            weak = (res, "convention")

    # Nothing on a known platform. A strong page on unknown software still beats
    # anything weak — VCU answers at catalog.vcu.edu but its CourseLeaf catalog
    # lives at bulletin.vcu.edu, and first-answer-wins picked the wrong one.
    if strong_other is not None:
        return accept(strong_other, "convention")

    link, home_res = homepage_catalog_link(fetcher, home)
    if link and fetcher.allowed(link):
        res = fetcher.get(link)
        grade = catalog_strength(res)
        if grade == "strong":
            return accept(res, "homepage_link")
        if grade == "weak" and weak is None:
            weak = (res, "homepage_link")

    # Nothing strong. Follow one hop off the best weak page — landing pages that
    # link to the real catalog are the single most common near-miss.
    if weak is not None:
        better = refine(fetcher, weak[0])
        if better is not None:
            return accept(better, weak[1] + "+refine", refined_from=weak[0]["url"])
        return accept(weak[0], weak[1] + "_weak")

    # No weak page either. Try refining the homepage itself.
    if home_res.get("status") == 200:
        better = refine(fetcher, home_res)
        if better is not None:
            return accept(better, "homepage+refine", refined_from=home_res["url"])

        # Last resort: follow the site's own signposting, homepage links first,
        # then academics and registrar hubs. This is where schools without a
        # single conventional catalog URL actually keep theirs.
        found, via = deep_scan(fetcher, home, home_res)
        if found is not None:
            return accept(found, via, refined_from=home_res["url"])

    out["status"] = home_res.get("status", 0)
    hs = home_res.get("status", 0)
    if hs in (401, 403, 405, 406, 429) or (hs == 0 and home_res.get("error")):
        # The homepage itself never loaded. That is a blocked crawler or a dead
        # host, not a school without a catalog, and the two must not be conflated.
        out["error"] = f"homepage unreachable: {home_res.get('error') or f'HTTP {hs}'}"
    else:
        out["error"] = home_res.get("error", "") or "no catalog found"
    if blocked:
        out["robots_blocked"] = True
        out["error"] = (out["error"] + f"; {blocked} candidates blocked by robots").strip("; ")
    return out


# ---------------------------------------------------------------- run

def cache_report(cache_dir):
    """READ-ONLY. How many cached pages are transient failures that V14 stored
    and V15 will refetch, by status and by host."""
    from collections import Counter
    by_status, by_host, total = Counter(), Counter(), 0
    for fn in os.listdir(cache_dir):
        if not fn.endswith(".json.gz"):
            continue
        total += 1
        try:
            with gzip.open(os.path.join(cache_dir, fn), "rt", encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            by_status["torn"] += 1
            continue
        st = d.get("status")
        if not Fetcher.cacheable(st):
            by_status[st] += 1
            by_host[urllib.parse.urlparse(d.get("url", "")).netloc] += 1
    bad = sum(by_status.values())
    print(f"{total:,} cached pages; {bad:,} are transient failures V15 will refetch")
    for st, n in by_status.most_common():
        print(f"  status {st}: {n:,}")
    print("  hosts with the most:")
    for h, n in by_host.most_common(25):
        print(f"    {n:>5}  {h}")


def main():
    p = argparse.ArgumentParser(description="Find catalogs and fingerprint platforms.")
    p.add_argument("--roster", default="data/roster.json")
    p.add_argument("--out-dir", default="data")
    p.add_argument("--cache-dir", default="cache/discover")
    p.add_argument("--all", action="store_true",
                   help="full census; default is the ranking tier only")
    p.add_argument("--limit", type=int, default=0, help="first N institutions, for a test run")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--delay", type=float, default=1.0, help="seconds between hits per domain")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--contact", default=DEFAULT_CONTACT)
    p.add_argument("--overrides", default="data/catalog_overrides.json",
                   help="unitid -> catalog url, for schools the crawler cannot find")
    p.add_argument("--edition", default="",
                   help="expected catalog edition, e.g. 2026-2027; "
                        "defaults to the current academic year")
    p.add_argument("--no-solve", action="store_true",
                   help="do not use a browser to clear JavaScript challenges")
    p.add_argument("--cache-report", action="store_true",
                   help="read-only: count cached transient failures by host")
    p.add_argument("--refresh", action="store_true",
                   help="ignore cached pages and refetch; use after changing "
                        "anything that runs at fetch time")
    p.add_argument("--insecure", action="store_true",
                   help="skip TLS verification; recorded in the log if used")
    a = p.parse_args()
    if a.cache_report:
        cache_report(a.cache_dir)
        return

    global CURRENT_EDITION
    now = datetime.now(timezone(timedelta(hours=-10)))
    start = now.year if now.month >= 7 else now.year - 1
    CURRENT_EDITION = a.edition or f"{start}-{start + 1}"

    log(f"{MARKER} — run {hst_today()} HST")
    log(f"  expecting catalog edition {CURRENT_EDITION}")
    with open(a.roster, encoding="utf-8") as fh:
        roster = json.load(fh)

    insts = roster["institutions"]
    if not a.all:
        tier = [i for i in insts if i.get("in_ranking_tier")]
        if tier:
            insts = tier
        else:
            log("  no in_ranking_tier set in roster; running the full census")
    if a.limit:
        insts = insts[:a.limit]

    log(f"  {len(insts)} institutions to discover")
    log(f"  {a.workers} workers, {a.delay}s per domain, contact {a.contact}")
    if a.insecure:
        log("  WARNING: TLS verification disabled for this run")

    overrides = load_overrides(a.overrides)
    if overrides:
        log(f"  {len(overrides)} manual catalog overrides loaded from {a.overrides}")

    fetcher = Fetcher(a.cache_dir, a.contact, a.delay, a.timeout, a.insecure,
                      refresh=a.refresh, solve_challenges=not a.no_solve)
    if a.refresh:
        log("  --refresh: ignoring cached pages, refetching everything")
    results, done, t0 = [], 0, time.time()

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(discover_one, fetcher, i, overrides): i for i in insts}
        for f in as_completed(futs):
            inst = futs[f]
            try:
                results.append(f.result())
            except Exception as e:
                results.append({
                    "unitid": inst["unitid"], "name": inst["name"],
                    "platform": "unknown", "confidence": "none",
                    "error": f"{type(e).__name__}: {e}"[:180],
                    "catalog_url": "", "found_via": "none", "evidence": [],
                })
            done += 1
            if done % 25 == 0 or done == len(insts):
                log(f"  {done}/{len(insts)}  ({time.time()-t0:.0f}s)")

    results.sort(key=lambda r: -r.get("completions", 0))

    found = [r for r in results if r["catalog_url"]]
    census = Counter(r["platform"] for r in results)
    via = Counter(r["found_via"] for r in results)
    conf = Counter(r["confidence"] for r in found)

    log("\nPLATFORM CENSUS  [this is the number that sizes harvest.py]")
    for plat, n in census.most_common():
        log(f"  {plat:<14} {n:>5}   {n/len(results)*100:5.1f}%")
    log(f"\n  catalog found for {len(found)}/{len(results)} "
        f"({len(found)/len(results)*100:.1f}%)")
    log("\nHow the catalog was located")
    for k, n in via.most_common():
        log(f"  {k:<14} {n:>5}")
    log("\nFingerprint confidence, among catalogs found")
    for k, n in conf.most_common():
        log(f"  {k:<14} {n:>5}")
    years = Counter(r.get("catalog_year") or "not stated"
                    for r in results if r["catalog_url"])
    log("\nCATALOG EDITION  [a stale edition dates the whole study]")
    for y, n in sorted(years.items(), key=lambda kv: (kv[0] != "not stated", kv[0]),
                       reverse=True):
        log(f"  {y:<14} {n:>5}")
    stale = [r for r in results if r.get("catalog_year")
             and r["catalog_year"] < CURRENT_EDITION]
    if stale:
        log(f"  {len(stale)} catalogs older than {CURRENT_EDITION} — REVIEW:")
        for r in stale[:15]:
            log(f"     {r['catalog_year']}  {r['name'][:38]:<38} {r['catalog_url'][:60]}")

    render = Counter(r.get("render") or "unknown" for r in results if r["catalog_url"])
    log("\nRENDER MODE  [client-rendered catalogs need an API adapter, not a scraper]")
    for k, n in render.most_common():
        log(f"  {k:<14} {n:>5}")
    spa = [r for r in results if r.get("render") == "client"]
    if spa:
        log("  client-rendered:")
        for r in spa[:15]:
            log(f"     {r['name'][:38]:<38} {r['platform']:<12} {r['catalog_url'][:52]}")

    misses = [r for r in results if not r["catalog_url"]]
    if misses:
        blockedm = [r for r in misses if "unreachable" in (r.get("error") or "")]
        genuine = [r for r in misses if r not in blockedm]
        log(f"\n  {len(misses)} not found")
        if blockedm:
            log(f"  {len(blockedm)} because the homepage itself never loaded "
                f"(blocked crawler or dead host, NOT a missing catalog):")
            for r in blockedm:
                log(f"     {r['unitid']}  {r['name'][:36]:<36} {r.get('error','')[:52]}")
        if genuine:
            log(f"  {len(genuine)} reachable but no catalog located "
                f"— candidates for catalog_overrides.json:")
            for r in genuine:
                log(f"     {r['unitid']}  {r['name'][:36]:<36} {r.get('error','')[:52]}")

    other = [r for r in results if r["platform"] == "other" and r["catalog_url"]]
    if other:
        log(f"\n  {len(other)} catalogs on unrecognized software — check for a new adapter:")
        for r in other[:15]:
            log(f"     {r['name'][:38]:<38} {r['catalog_url'][:66]}")

    weak_accepts = [r for r in results if r["found_via"].endswith("_weak")]
    refined = [r for r in results if "refine" in r["found_via"]]
    log(f"\n  {len(refined)} catalogs found by following one hop off a landing page")
    if weak_accepts:
        log(f"  {len(weak_accepts)} weak accepts — REVIEW THESE BY HAND:")
        for r in weak_accepts[:15]:
            log(f"     {r['name'][:40]:<40} {r['catalog_url'][:70]}")
    blocked = sum(1 for r in results if r.get("robots_blocked"))
    if blocked:
        log(f"\n  {blocked} institutions had candidates blocked by robots.txt")

    cover = sum(1 for r in results[:50] if r["catalog_url"])
    log(f"\n  top 50 by completions: {cover}/50 catalogs found")

    os.makedirs(a.out_dir, exist_ok=True)
    jp = os.path.join(a.out_dir, "catalogs.json")
    with open(jp, "w", encoding="utf-8") as fh:
        json.dump({
            "marker": MARKER,
            "generated": hst_today(),
            "generated_tz": "HST",
            "roster": os.path.basename(a.roster),
            "scope": "all" if a.all else "ranking_tier",
            "measures": "catalog location and platform only; no curriculum is read here",
            "contact": a.contact,
            "current_edition_expected": CURRENT_EDITION,
            "catalog_edition_census": dict(years),
            "render_census": dict(render),
            "platform_census": dict(census),
            "found_via": dict(via),
            "n_institutions": len(results),
            "n_catalogs_found": len(found),
            "institutions": results,
        }, fh, indent=2, ensure_ascii=False)
    log(f"\nWrote {jp}")

    cp = os.path.join(a.out_dir, "catalogs.csv")
    with open(cp, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["unitid", "name", "state", "control", "completions",
                    "catalog_url", "platform", "confidence", "catalog_year",
                    "render", "found_via", "refined_from", "status", "error"])
        for r in results:
            w.writerow([r.get("unitid"), r.get("name"), r.get("state"),
                        r.get("control"), r.get("completions"),
                        r.get("catalog_url"), r.get("platform"),
                        r.get("confidence"), r.get("catalog_year", ""),
                        r.get("render", ""), r.get("found_via"),
                        r.get("refined_from", ""),
                        r.get("status"), r.get("error")])
    log(f"Wrote {cp}")

    mp = os.path.join(a.out_dir, "misses.csv")
    with open(mp, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["unitid", "name", "state", "control", "completions",
                    "homepage", "error", "catalog_url_paste_here"])
        for r in sorted(misses, key=lambda r: -r.get("completions", 0)):
            w.writerow([r.get("unitid"), r.get("name"), r.get("state"),
                        r.get("control"), r.get("completions"),
                        r.get("homepage"), r.get("error"), ""])
    log(f"Wrote {mp}  (fill the last column, then convert to overrides)")

    lp = os.path.join(a.out_dir, "discover_run.log")
    with open(lp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(LOG) + "\n")
    log(f"Wrote {lp}")


if __name__ == "__main__":
    main()
