"""
Transfermarkt Mirror — Comprehensive scraper into MongoDB (localhost/markt).

Usage:
    python3 markt.py                     # all leagues, current season
    python3 markt.py --league L1         # single league
    python3 markt.py --season 2023       # historical season
    python3 markt.py --skip-matches      # skip match detail scraping (faster)
    python3 markt.py --force             # ignore 24h cache, re-scrape everything
"""

import argparse
import random
import re
import time
from datetime import datetime, timezone

import pymongo
import requests
from bs4 import BeautifulSoup

# ─── Config ──────────────────────────────────────────────────────────────────

BASE = "https://www.transfermarkt.de"

LEAGUES = {
    "L1": "Bundesliga",
    "L2": "2. Bundesliga",
    "GB1": "Premier League",
    "GB2": "Championship",
    "ES1": "La Liga",
    "IT1": "Serie A",
    "FR1": "Ligue 1",
    "NL1": "Eredivisie",
    "PO1": "Liga Portugal",
    "TR1": "Süper Lig",
    "MLS1": "MLS",
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
]


# ─── Scraper ─────────────────────────────────────────────────────────────────

MAX_AGE = 86400  # 24h — skip documents fresher than this (seconds)


class TransfermarktMirror:
    def __init__(self, db_uri="mongodb://localhost:27017/", max_age=MAX_AGE):
        self.client = pymongo.MongoClient(db_uri)
        self.db = self.client["markt"]
        self.max_age = max_age
        self._ensure_indexes()
        self.session = requests.Session()
        self._stats = {"requests": 0, "errors": 0, "skipped": 0}

    # ── Indexes ──────────────────────────────────────────────────────────

    def _ensure_indexes(self):
        self.db.leagues.create_index("tm_id", unique=True)
        self.db.clubs.create_index("tm_id", unique=True)
        self.db.clubs.create_index("league_id")
        self.db.players.create_index("tm_id", unique=True)
        self.db.players.create_index("club_id")
        self.db.transfers.create_index([("player_id", 1), ("season", 1), ("to_club_id", 1)])
        self.db.matches.create_index("tm_id", unique=True)
        self.db.matches.create_index([("league_id", 1), ("season", 1)])
        self.db.officials.create_index("tm_id", unique=True)

    # ── Freshness check ────────────────────────────────────────────────

    def _is_fresh(self, collection, query):
        """Return True if a document matching `query` was updated within max_age."""
        if self.max_age <= 0:
            return False
        doc = collection.find_one(query, {"last_updated": 1})
        if not doc or not doc.get("last_updated"):
            return False
        lu = doc["last_updated"]
        if lu.tzinfo is None:
            lu = lu.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - lu).total_seconds()
        return age < self.max_age

    # ── HTTP ─────────────────────────────────────────────────────────────

    def _get_soup(self, url):
        time.sleep(random.uniform(3.0, 6.0))
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.transfermarkt.de/",
        }
        try:
            res = self.session.get(url, headers=headers, timeout=20)
            self._stats["requests"] += 1
            if res.status_code == 429:
                print("  [!] Rate limited — sleeping 30s")
                time.sleep(30)
                return self._get_soup(url)  # retry once
            res.raise_for_status()
            return BeautifulSoup(res.content, "html.parser")
        except Exception as e:
            self._stats["errors"] += 1
            print(f"  [ERR] {url}: {e}")
            return None

    # ── Value parsing ────────────────────────────────────────────────────

    @staticmethod
    def _clean_mv(val):
        """'€12,00 Mio. €' / '€500 Tsd. €' / '€150.00m' → int euros."""
        if not val:
            return 0
        val = val.strip()
        if not val or val == "-" or "?" in val:
            return 0
        val_lower = val.lower()
        if "mrd" in val_lower:
            mult = 1_000_000_000
        elif "mio" in val_lower or "mill" in val_lower or "m" in val_lower:
            mult = 1_000_000
        elif "tsd" in val_lower or "k" in val_lower:
            mult = 1_000
        else:
            mult = 1
        num = re.sub(r"[^\d,.]", "", val).replace(",", ".").strip(".")
        try:
            return int(float(num) * mult)
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _clean_fee(val):
        """Transfer fee text → int euros, or special string ('Leihe', 'ablösefrei')."""
        if not val:
            return 0
        val = val.strip()
        low = val.lower()
        if "ablösefrei" in low or "free" in low:
            return "free"
        if "leih" in low or "loan" in low:
            return "loan"
        if "?" in val or "-" == val:
            return 0
        return TransfermarktMirror._clean_mv(val)

    @staticmethod
    def _parse_date(text):
        """Try to parse German date strings like '15.08.2024' or 'Aug 15, 2024'."""
        if not text:
            return None
        text = text.strip()
        for fmt in ("%d.%m.%Y", "%d. %b %Y", "%b %d, %Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None

    # ── League ───────────────────────────────────────────────────────────

    def scrape_league(self, league_id, season):
        label = LEAGUES.get(league_id, league_id)
        print(f"\n{'='*60}")
        print(f"  LEAGUE: {label} ({league_id}) — Season {season}/{int(season)+1}")
        print(f"{'='*60}")

        url = f"{BASE}/_/startseite/wettbewerb/{league_id}/plus/?saison_id={season}"
        soup = self._get_soup(url)
        if not soup:
            return []

        # League metadata
        h1 = soup.find("h1")
        league_name = h1.get_text(strip=True) if h1 else label

        # Total market value from the header area
        total_mv = 0
        mv_span = soup.find("span", class_="waehrung")
        if mv_span:
            total_mv = self._clean_mv(mv_span.parent.get_text())

        # Count clubs from the table
        table = soup.find("table", class_="items")
        club_links = []
        if table:
            for row in table.find_all("td", class_="hauptlink"):
                link = row.find("a")
                if link and link.get("href") and "/verein/" in link["href"]:
                    m = re.search(r"verein/(\d+)", link["href"])
                    if m:
                        club_links.append((m.group(1), link.text.strip()))

        self.db.leagues.update_one(
            {"tm_id": league_id},
            {"$set": {
                "tm_id": league_id,
                "name": league_name,
                "season": season,
                "total_market_value": total_mv,
                "num_clubs": len(club_links),
                "last_updated": datetime.now(timezone.utc),
            }},
            upsert=True,
        )

        print(f"  Found {len(club_links)} clubs")

        # Scrape each club
        club_ids = []
        for i, (club_id, club_name) in enumerate(club_links, 1):
            print(f"  [{i}/{len(club_links)}] Club: {club_name} (tm_id={club_id})")
            self._scrape_club_squad(club_id, league_id, season)
            club_ids.append(club_id)

        return club_ids

    # ── Club + Squad ─────────────────────────────────────────────────────

    def _scrape_club_squad(self, club_id, league_id, season):
        """Scrape detailed squad page (/kader/verein/{id}/plus/1)."""
        if self._is_fresh(self.db.clubs, {"tm_id": club_id}):
            cached = self.db.clubs.find_one({"tm_id": club_id}, {"name": 1})
            print(f"    [SKIP] {cached.get('name', club_id)} — fresh")
            self._stats["skipped"] += 1
            return

        url = f"{BASE}/_/kader/verein/{club_id}/saison_id/{season}/plus/1"
        soup = self._get_soup(url)
        if not soup:
            return

        # Club name
        h1 = soup.find("h1")
        club_name = h1.get_text(strip=True) if h1 else f"Club {club_id}"

        # Squad total MV + avg age from the header stats
        squad_size = 0
        avg_age = None
        total_mv = 0

        # Look for the stats in the squad header
        stat_items = soup.find_all("li", class_="data-header__label")
        for item in stat_items:
            text = item.get_text(strip=True).lower()
            val_span = item.find("span", class_="data-header__content")
            val = val_span.get_text(strip=True) if val_span else ""
            if "kader" in text or "squad" in text or "size" in text:
                nums = re.findall(r"\d+", val)
                if nums:
                    squad_size = int(nums[0])
            elif "alter" in text or "age" in text:
                age_match = re.search(r"[\d,.]+", val)
                if age_match:
                    avg_age = float(age_match.group().replace(",", "."))

        # Total MV from the header
        mv_el = soup.find("a", class_="data-header__market-value-wrapper")
        if mv_el:
            total_mv = self._clean_mv(mv_el.get_text())

        self.db.clubs.update_one(
            {"tm_id": club_id},
            {"$set": {
                "tm_id": club_id,
                "name": club_name,
                "league_id": league_id,
                "season": season,
                "squad_size": squad_size,
                "avg_age": avg_age,
                "total_market_value": total_mv,
                "last_updated": datetime.now(timezone.utc),
            }},
            upsert=True,
        )

        # Players from the detailed squad table
        player_count = 0
        rows = soup.find_all("tr", class_=["odd", "even"])
        for row in rows:
            player = self._parse_player_row(row, club_id)
            if player:
                self.db.players.update_one(
                    {"tm_id": player["tm_id"]},
                    {"$set": player},
                    upsert=True,
                )
                player_count += 1

        print(f"    → {player_count} players, MV: {total_mv:,}€")

    def _parse_player_row(self, row, club_id):
        """Parse a single player row from the detailed squad table."""
        # Find player link
        hauptlink = row.find("td", class_="hauptlink")
        if not hauptlink:
            return None
        p_link = hauptlink.find("a")
        if not p_link or not p_link.get("href") or "/spieler/" not in p_link["href"]:
            return None

        m = re.search(r"spieler/(\d+)", p_link["href"])
        if not m:
            return None
        p_id = m.group(1)

        # All cells
        cells = row.find_all("td")

        # Market value (rightmost hauptlink cell)
        mv_cell = row.find("td", class_="rechts hauptlink")
        market_value = self._clean_mv(mv_cell.get_text(strip=True)) if mv_cell else 0

        # Position — usually in the second hauptlink or in a specific cell
        position = ""
        pos_cells = row.find_all("td", class_="posrela")
        if pos_cells:
            pos_span = pos_cells[0].find("td", class_="pos-left") or pos_cells[0].find(class_="pos")
            if pos_span:
                position = pos_span.get_text(strip=True)

        # Try inline-table for position
        if not position:
            inline = row.find("table", class_="inline-table")
            if inline:
                pos_tr = inline.find_all("tr")
                if len(pos_tr) > 1:
                    position = pos_tr[1].get_text(strip=True)

        # Nationality — flag images
        nationalities = []
        flag_imgs = row.find_all("img", class_="flaggenrahmen")
        for img in flag_imgs:
            nat = img.get("title", "").strip()
            if nat:
                nationalities.append(nat)

        # Date of birth
        dob = None
        dob_text = ""
        for cell in cells:
            text = cell.get_text(strip=True)
            # German DOB format: "15.08.1995 (29)" or just "15.08.1995"
            dob_match = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
            if dob_match:
                dob_text = dob_match.group(1)
                dob = self._parse_date(dob_text)
                break

        # Age
        age = None
        age_match = re.search(r"\((\d+)\)", row.get_text())
        if age_match:
            age = int(age_match.group(1))

        # Shirt number
        shirt = None
        nr_cell = row.find("div", class_="rn_nummer")
        if nr_cell:
            nr_text = nr_cell.get_text(strip=True)
            if nr_text.isdigit():
                shirt = int(nr_text)

        # Height
        height = None
        for cell in cells:
            h_match = re.search(r"(\d[,.]\d{2})\s*m", cell.get_text())
            if h_match:
                height = h_match.group(1).replace(",", ".")
                break

        # Foot
        foot = None
        for cell in cells:
            text = cell.get_text(strip=True).lower()
            if text in ("rechts", "links", "beidfüßig", "right", "left", "both"):
                foot = text
                break

        # Contract expiry
        contract = None
        for cell in cells:
            # Look for future date pattern that's likely a contract
            c_match = re.search(r"(\d{2}\.\d{2}\.\d{4})", cell.get_text())
            if c_match and c_match.group(1) != dob_text:
                parsed = self._parse_date(c_match.group(1))
                if parsed and (not dob or parsed > dob):
                    contract = parsed

        return {
            "tm_id": p_id,
            "name": p_link.get_text(strip=True),
            "club_id": club_id,
            "position": position,
            "nationality": nationalities,
            "date_of_birth": dob,
            "age": age,
            "height": height,
            "foot": foot,
            "shirt_number": shirt,
            "market_value": market_value,
            "contract_expires": contract,
            "last_updated": datetime.now(timezone.utc),
        }

    # ── Stadium ──────────────────────────────────────────────────────────

    def scrape_stadium(self, club_id):
        # Skip if club already has stadium data
        existing = self.db.clubs.find_one({"tm_id": club_id}, {"stadium_name": 1})
        if existing and existing.get("stadium_name"):
            self._stats["skipped"] += 1
            return

        url = f"{BASE}/_/stadion/verein/{club_id}"
        soup = self._get_soup(url)
        if not soup:
            return

        h1 = soup.find("h1")
        stadium_name = h1.get_text(strip=True) if h1 else None

        capacity = None
        # Look for capacity in data fields
        for item in soup.find_all("li", class_="data-header__label"):
            text = item.get_text(strip=True).lower()
            if "kapazität" in text or "capacity" in text:
                content = item.find("span", class_="data-header__content")
                if content:
                    nums = re.findall(r"[\d.]+", content.get_text().replace(".", ""))
                    if nums:
                        capacity = int(nums[0])

        # Also try the info table
        if not capacity:
            for row in soup.find_all("tr"):
                label = row.find("th")
                val = row.find("td")
                if label and val:
                    if "kapazität" in label.get_text(strip=True).lower():
                        nums = re.findall(r"[\d.]+", val.get_text().replace(".", ""))
                        if nums:
                            capacity = int(nums[0])

        update = {}
        if stadium_name:
            update["stadium_name"] = stadium_name
        if capacity:
            update["stadium_capacity"] = capacity

        if update:
            self.db.clubs.update_one({"tm_id": club_id}, {"$set": update})
            print(f"    → Stadium: {stadium_name} ({capacity:,} seats)" if capacity else f"    → Stadium: {stadium_name}")

    # ── Transfers ────────────────────────────────────────────────────────

    def scrape_transfers(self, league_id, season):
        print(f"\n  Transfers for {league_id} ({season})...")
        for window, slug in [("summer", "sommertransfers"), ("winter", "wintertransfers")]:
            # Skip if we already have fresh transfers for this league+season+window
            if self._is_fresh(self.db.transfers, {"league_id": league_id, "season": season, "window": window}):
                count = self.db.transfers.count_documents({"league_id": league_id, "season": season, "window": window})
                print(f"    [SKIP] {window}: {count} transfers — fresh")
                self._stats["skipped"] += 1
                continue

            url = f"{BASE}/_/{slug}/wettbewerb/{league_id}/plus/?saison_id={season}"
            soup = self._get_soup(url)
            if not soup:
                continue

            count = 0
            # Transfer tables — each club has a table
            tables = soup.find_all("table", class_="items")
            for table in tables:
                rows = table.find_all("tr", class_=["odd", "even"])
                for row in rows:
                    transfer = self._parse_transfer_row(row, league_id, season, window)
                    if transfer:
                        self.db.transfers.update_one(
                            {
                                "player_id": transfer["player_id"],
                                "season": transfer["season"],
                                "window": transfer["window"],
                                "to_club_id": transfer.get("to_club_id"),
                            },
                            {"$set": transfer},
                            upsert=True,
                        )
                        count += 1

            print(f"    → {window}: {count} transfers")

    def _parse_transfer_row(self, row, league_id, season, window):
        """Parse a single transfer row."""
        cells = row.find_all("td")
        if len(cells) < 3:
            return None

        # Player link
        p_link = row.find("a", href=lambda x: x and "/spieler/" in x)
        if not p_link:
            return None
        m = re.search(r"spieler/(\d+)", p_link["href"])
        if not m:
            return None
        player_id = m.group(1)
        player_name = p_link.get_text(strip=True)

        # From/To club links
        club_links = row.find_all("a", href=lambda x: x and "/verein/" in x)
        from_club_id = None
        to_club_id = None
        from_club_name = None
        to_club_name = None
        for cl in club_links:
            cm = re.search(r"verein/(\d+)", cl["href"])
            if cm:
                if from_club_id is None:
                    from_club_id = cm.group(1)
                    from_club_name = cl.get_text(strip=True)
                else:
                    to_club_id = cm.group(1)
                    to_club_name = cl.get_text(strip=True)

        # Fee — usually the last cell with a value
        fee = 0
        fee_cell = row.find("td", class_="rechts")
        if fee_cell:
            fee = self._clean_fee(fee_cell.get_text(strip=True))

        # MV at transfer
        mv_cell = row.find("td", class_="rechts hauptlink")
        mv_at_transfer = self._clean_mv(mv_cell.get_text(strip=True)) if mv_cell else 0

        # Age
        age = None
        for cell in cells:
            text = cell.get_text(strip=True)
            if text.isdigit() and 14 <= int(text) <= 50:
                age = int(text)
                break

        # Position
        position = ""
        pos_cell = row.find("td", class_="pos-center")
        if pos_cell:
            position = pos_cell.get_text(strip=True)

        return {
            "player_id": player_id,
            "player_name": player_name,
            "age": age,
            "position": position,
            "from_club_id": from_club_id,
            "from_club_name": from_club_name,
            "to_club_id": to_club_id,
            "to_club_name": to_club_name,
            "fee": fee,
            "market_value_at_transfer": mv_at_transfer,
            "season": season,
            "window": window,
            "league_id": league_id,
            "last_updated": datetime.now(timezone.utc),
        }

    # ── Matches ──────────────────────────────────────────────────────────

    def scrape_season_matches(self, league_id, season):
        print(f"\n  Match schedule for {league_id} ({season})...")
        url = f"{BASE}/_/gesamtspielplan/wettbewerb/{league_id}/saison_id/{season}"
        soup = self._get_soup(url)
        if not soup:
            return

        scraped = 0
        skipped = 0

        # Find all match result links
        for link in soup.find_all("a", href=lambda x: x and "/spielbericht/" in x):
            m = re.search(r"spielbericht/(\d+)", link["href"])
            if not m:
                continue
            match_id = m.group(1)

            if self._is_fresh(self.db.matches, {"tm_id": match_id}):
                skipped += 1
                self._stats["skipped"] += 1
                continue

            self._scrape_match_detail(match_id, league_id, season)
            scraped += 1

        print(f"    → {scraped} scraped, {skipped} skipped (fresh)")

    def _scrape_match_detail(self, match_id, league_id, season):
        url = f"{BASE}/spielbericht/index/spielbericht/{match_id}"
        soup = self._get_soup(url)
        if not soup:
            return

        # Teams
        team_links = soup.find_all("a", class_="sb-vereinslink")
        home_team_id = None
        away_team_id = None
        home_team_name = None
        away_team_name = None
        for i, tl in enumerate(team_links):
            m = re.search(r"verein/(\d+)", tl.get("href", ""))
            if m:
                if i == 0:
                    home_team_id = m.group(1)
                    home_team_name = tl.get_text(strip=True)
                elif i == 1:
                    away_team_id = m.group(1)
                    away_team_name = tl.get_text(strip=True)

        # Score
        score_el = soup.find("div", class_="sb-endstand") or soup.find("span", class_="sb-endstand")
        home_score = None
        away_score = None
        if score_el:
            score_text = score_el.get_text(strip=True)
            score_match = re.search(r"(\d+)\s*:\s*(\d+)", score_text)
            if score_match:
                home_score = int(score_match.group(1))
                away_score = int(score_match.group(2))

        # Referee
        ref_link = soup.find("a", href=lambda x: x and "/schiedsrichter/" in x)
        ref_id = None
        ref_name = None
        if ref_link:
            rm = re.search(r"schiedsrichter/(\d+)", ref_link["href"])
            if rm:
                ref_id = rm.group(1)
                ref_name = ref_link.get_text(strip=True)
                self.db.officials.update_one(
                    {"tm_id": ref_id},
                    {"$set": {"tm_id": ref_id, "name": ref_name, "last_updated": datetime.now(timezone.utc)}},
                    upsert=True,
                )

        # Match date
        match_date = None
        date_box = soup.find("p", class_="sb-datum")
        if date_box:
            date_match = re.search(r"(\d{2}\.\d{2}\.\d{4})", date_box.get_text())
            if date_match:
                match_date = self._parse_date(date_match.group(1))

        # Matchday
        matchday = None
        md_match = re.search(r"(\d+)\.\s*Spieltag", soup.get_text())
        if md_match:
            matchday = int(md_match.group(1))

        # Attendance
        attendance = None
        for el in soup.find_all(["p", "span", "td"]):
            text = el.get_text(strip=True)
            att_match = re.search(r"Zuschauer[:\s]*([.\d]+)", text)
            if att_match:
                attendance = int(att_match.group(1).replace(".", ""))
                break

        # Stadium
        stadium = None
        stadium_link = soup.find("a", href=lambda x: x and "/stadion/" in x)
        if stadium_link:
            stadium = stadium_link.get_text(strip=True)

        self.db.matches.update_one(
            {"tm_id": match_id},
            {"$set": {
                "tm_id": match_id,
                "league_id": league_id,
                "season": season,
                "matchday": matchday,
                "date": match_date,
                "home_team_id": home_team_id,
                "home_team_name": home_team_name,
                "away_team_id": away_team_id,
                "away_team_name": away_team_name,
                "home_score": home_score,
                "away_score": away_score,
                "referee_id": ref_id,
                "referee_name": ref_name,
                "attendance": attendance,
                "stadium": stadium,
                "last_updated": datetime.now(timezone.utc),
            }},
            upsert=True,
        )

    # ── Full pipeline ────────────────────────────────────────────────────

    def run(self, league_ids, season, skip_matches=False):
        print(f"Transfermarkt Mirror — Season {season}/{int(season)+1}")
        print(f"Leagues: {', '.join(league_ids)}")
        print(f"Target DB: {self.db.name}")
        print()

        for league_id in league_ids:
            # 1. League overview + clubs + players
            club_ids = self.scrape_league(league_id, season)

            # 2. Stadiums for each club
            print(f"\n  Stadiums for {league_id}...")
            for cid in club_ids:
                self.scrape_stadium(cid)

            # 3. Transfers
            self.scrape_transfers(league_id, season)

            # 4. Match results + referees
            if not skip_matches:
                self.scrape_season_matches(league_id, season)

            print(f"\n  ✓ {league_id} complete")

        # Summary
        print(f"\n{'='*60}")
        print(f"  DONE — {self._stats['requests']} requests, {self._stats['skipped']} skipped, {self._stats['errors']} errors")
        print(f"  DB: {self.db.leagues.count_documents({})} leagues, "
              f"{self.db.clubs.count_documents({})} clubs, "
              f"{self.db.players.count_documents({})} players, "
              f"{self.db.transfers.count_documents({})} transfers, "
              f"{self.db.matches.count_documents({})} matches, "
              f"{self.db.officials.count_documents({})} officials")
        print(f"{'='*60}")


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Transfermarkt → MongoDB mirror")
    parser.add_argument("--league", type=str, help="Single league ID (e.g. L1, GB1)")
    parser.add_argument("--season", type=str, default="2020", help="Season start year (default: 2020)")
    parser.add_argument("--skip-matches", action="store_true", help="Skip match detail scraping")
    parser.add_argument("--force", action="store_true", help="Ignore freshness cache, re-scrape everything")
    args = parser.parse_args()

    leagues = [args.league] if args.league else list(LEAGUES.keys())

    bot = TransfermarktMirror(max_age=0 if args.force else MAX_AGE)
    bot.run(leagues, args.season, skip_matches=args.skip_matches)
