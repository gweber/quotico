# 🚀 PROMPT: System Architecture Audit & Gap Analysis

**Rolle:** Senior Systems Architect & QA Lead
**Ziel:** Analyse des aktuellen Implementierungsstatus gegenüber der `AGENTS.md` Verfassung. Identifikation von "Broken Windows" (Verstöße gegen Greenfield-Regeln) und fehlenden Verbindungen zwischen den Towers (Team, League, Match).

---

### 1. Model & Schema Audit
Untersuche die Dateien in `backend/app/models/` auf folgende **Inkonsistenzen**:
- **Team Tower:** Existieren noch Felder wie `team_key` oder `legacy_id` in `teams.py`?
- **League Tower:** Enthält `leagues.py` das `features`-Objekt (`tipping`, `match_load`) und `structure_type` (`cup`, `league`)?
- **Match Tower:**
    - Ist `odds` als strukturiertes Objekt (`primary` + `providers`) definiert oder noch flach?
    - Gibt es noch `home_team_name` Strings im Match-Model (außer für UI-Caching)?
    - Ist `round_name` für Pokale vorhanden?
- **Bets:** Existiert das `Bet`-Modell getrennt vom Match?

### 2. Logic & Flow Audit (Static Analysis)
Prüfe die Import-Logik in `backend/app/services/` und `backend/app/providers/`:
- **Provider-Isolation:** Nutzen `TheOddsAPI` und `FootballData` Provider die `LeagueRegistry`, um externe IDs aufzulösen (`external_ids` Mapping)? Oder sind IDs hardcodiert?
- **Team Resolution:** Wird in *jedem* Ingest-Pfad (Matches, xG, Odds) `TeamRegistry.resolve()` aufgerufen?
    - **Suche nach:** Direkten Zuweisungen von String-Namen ohne Resolution -> 🚩 FAIL.
- **Admin Router:** Prüfe `backend/app/routers/admin.py`.
    - Sind die Endpoints `/teams/merge`, `/leagues/seed`, `/leagues/{id}/sync` vorhanden?
    - Gibt es noch alte Endpoints, die auf `team_mappings` verweisen?

### 3. The "Breaks" (Bruchstellen-Analyse)
Erstelle eine Liste von **TODOs / Lücken**, wo die neue Architektur noch nicht durchgezogen wurde. Achte besonders auf:
- **Timezone Safety:** Werden irgendwo `datetime.now()` ohne UTC genutzt?
- **Migration Leftovers:** Gibt es noch Skripte in `tools/`, die eigentlich in den Core (Services) gehören (z.B. Import-Skripte)?
- **Hardcoded Strings:** Findest du im Code Ligen-Keys wie `"soccer_epl"` an Stellen, wo eigentlich über die Registry iteriert werden sollte?

---

### Output Format (Markdown)

Erstelle einen **Status Report**:

**1. ✅ Green Zone (Erfolgreich umgesetzt)**
* [Liste der Komponenten, die den Greenfield-Status erfüllen]

**2. ⚠️ Yellow Zone (Wackelig / Unvollständig)**
* [Komponenten, die zwar da sind, aber noch alte Logik enthalten]
* [Beispiel: Provider nutzt noch Dictionary statt LeagueRegistry]

**3. 🛑 Red Zone (Legacy / Verstöße)**
* [Dateien/Funktionen, die `team_key` nutzen]
* [Skripte, die noch außerhalb von `backend/app` leben]
* [Provider, die Strings statt ObjectIds schreiben]

**4. Action Plan**
* Schlage die nächsten 3 konkreten Refactoring-Schritte vor, um die Red Zone zu eliminieren.