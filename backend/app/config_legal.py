"""Legal document configuration and content for Quotico.de.

Content is stored as pre-rendered HTML strings. Changing TERMS_VERSION
will cause all users to be prompted to re-accept the AGB on next login.
"""

TERMS_VERSION = "2.0"
TERMS_UPDATED_AT = "2026-02-22"

LEGAL_DOCS = {
    "imprint": {
        "title": "Impressum",
        "slug": "impressum",
        "content_html": """
<h2>Angaben gemäß § 5 DDG</h2>
<p><strong>[Vorname Nachname / Firmenname]</strong></p>
<p>
  [Straße Hausnummer]<br>
  [PLZ Ort]<br>
  Deutschland
</p>

<h3>Kontakt</h3>
<p>
  E-Mail: <a href="mailto:kontakt@quotico.de">kontakt@quotico.de</a><br>
  Telefon: [+49 XXX XXXXXXX]
</p>

<h3>Vertretungsberechtigte Person(en)</h3>
<p>[Vorname Nachname, Geschäftsführer/Inhaber]</p>

<h3>Registereintrag</h3>
<p>[Handelsregister: Amtsgericht [Ort], HRB [Nummer] — falls zutreffend]</p>

<h3>Umsatzsteuer-Identifikationsnummer / Wirtschafts-Identifikationsnummer</h3>
<p>[USt-IdNr. gemäß § 27a UStG: DE XXXXXXXXX — falls vorhanden]</p>
<p>[W-IdNr. gemäß § 139c AO — falls vorhanden]</p>

<h3>Verantwortlich für den Inhalt gemäß § 18 Abs. 2 MStV</h3>
<p>[Vorname Nachname]<br>[Anschrift wie oben]</p>

<h3>Haftungshinweis</h3>
<p>
  Quotico.de ist ein Tipspiel zur Unterhaltung. Es handelt sich ausdrücklich
  nicht um Glücksspiel im Sinne des Glücksspielstaatsvertrags (GlüStV).
  Es wird kein echtes Geld eingesetzt oder ausgezahlt.
</p>

<h3>EU-Streitschlichtung</h3>
<p>
  Die Europäische Kommission stellt eine Plattform zur Online-Streitbeilegung
  (OS) bereit. Wir sind weder verpflichtet noch bereit, an einem
  Streitbeilegungsverfahren vor einer Verbraucherschlichtungsstelle teilzunehmen.
</p>
""",
    },
    "privacy": {
        "title": "Datenschutzerklärung",
        "slug": "datenschutz",
        "content_html": """
<h2>1. Verantwortlicher</h2>
<p>
  [Vorname Nachname / Firmenname]<br>
  [Straße Hausnummer], [PLZ Ort]<br>
  E-Mail: <a href="mailto:kontakt@quotico.de">kontakt@quotico.de</a>
</p>

<h2>2. Überblick über die Datenverarbeitung</h2>
<p>
  Wir verarbeiten personenbezogene Daten nur, soweit dies zur Bereitstellung
  unseres Tippspiel-Dienstes erforderlich ist. Nachfolgend informieren wir
  Sie über Art, Umfang und Zweck der Datenverarbeitung.
</p>

<h2>3. Erhobene Daten</h2>
<ul>
  <li><strong>E-Mail-Adresse</strong> — zur Kontoerstellung und Kommunikation (Rechtsgrundlage: Art. 6 Abs. 1 lit. b DSGVO)</li>
  <li><strong>Alias / Anzeigename</strong> — öffentlich sichtbar im Leaderboard und in Squads (Art. 6 Abs. 1 lit. b DSGVO)</li>
  <li><strong>Geburtsdatum</strong> — wird nur zur einmaligen Altersverifikation (18+) verwendet und nicht dauerhaft gespeichert (Art. 6 Abs. 1 lit. c DSGVO, Jugendschutz)</li>
  <li><strong>Passwort</strong> — gespeichert als Argon2-Hash, niemals im Klartext (Art. 6 Abs. 1 lit. b DSGVO)</li>
  <li><strong>IP-Adressen</strong> — in Audit-Logs und Fingerprint-Datensätzen anonymisiert gespeichert (letztes Oktett durch „xxx" ersetzt). Vollständige IP-Adressen werden zu keinem Zeitpunkt in der Datenbank gespeichert, sondern ausschließlich in Server-Logdateien für max. 7 Tage vorgehalten (Art. 6 Abs. 1 lit. f DSGVO)</li>
  <li><strong>2FA-Geheimnisse</strong> — mit Fernet (AES-128-CBC) verschlüsselt gespeichert, mit Unterstützung für Schlüsselrotation (Art. 6 Abs. 1 lit. b DSGVO)</li>
  <li><strong>Google-OAuth-Daten</strong> — bei Nutzung der Google-Anmeldung: E-Mail-Adresse und Google-Sub-ID. Es werden keine Google-Zugangsdaten gespeichert (Art. 6 Abs. 1 lit. b DSGVO)</li>
  <li><strong>Tipps und Punkte</strong> — Ihre Vorhersagen (1/X/2-Tipps und Spieltag-Ergebnisprognosen), gesperrte Quoten und erzielte Punkte (Art. 6 Abs. 1 lit. b DSGVO)</li>
  <li><strong>Wallet-Daten</strong> — virtueller Kontostand, Einsätze, Transaktionshistorie und Spielmodus-Aktivitäten (Bankroll-Wetten, Survivor-Picks, Over/Under-Tipps, Fantasy-Picks, Kombi-Joker). Diese Daten haben keinen realen Geldwert. (Art. 6 Abs. 1 lit. b DSGVO)</li>
  <li><strong>Wallet-Disclaimer-Zustimmung</strong> — Zeitpunkt der Bestätigung, dass Quotico-Coins keinen realen Gegenwert haben (Art. 6 Abs. 1 lit. c DSGVO, Jugendschutz)</li>
  <li><strong>Geräte-Fingerprint (Hash)</strong> — ein SHA-256-Hashwert, der client-seitig aus allgemeinen Geräteeigenschaften (User-Agent, Bildschirmauflösung, Zeitzone, Sprache, Plattform) berechnet wird. <strong>Ausschließlich der Hash wird an den Server übermittelt und gespeichert</strong> — die einzelnen Geräteeigenschaften werden weder übertragen noch in der Datenbank gespeichert. Eine Rückberechnung der Einzelmerkmale aus dem Hash ist nicht möglich. (Art. 6 Abs. 1 lit. f DSGVO — berechtigtes Interesse: Schutz vor Mehrfach-Accounts und Manipulation)</li>
  <li><strong>Haushalts-Gruppierung</strong> — bei Übereinstimmung von IP-Adresse und Fingerprint-Hash über mehrere Konten wird automatisch eine anonyme Gruppen-ID vergeben. Dies dient der Erkennung von Mehrfach-Accounts und dem Schutz der Plattform-Integrität. Es werden dabei keine zusätzlichen personenbezogenen Daten erhoben. (Art. 6 Abs. 1 lit. f DSGVO)</li>
</ul>

<h2>4. Cookies</h2>
<p>
  Wir verwenden ausschließlich technisch notwendige Cookies:
</p>
<ul>
  <li><strong>access_token</strong> — httpOnly, kurzlebig (15 Minuten), zur Authentifizierung</li>
  <li><strong>refresh_token</strong> — httpOnly, 7 Tage Gültigkeit, zur Token-Erneuerung</li>
  <li><strong>session</strong> — httpOnly, 5 Minuten, ausschließlich für den Google-OAuth-CSRF-Schutz</li>
</ul>
<p>
  Es werden keine Tracking-, Analyse- oder Werbe-Cookies eingesetzt.
  Ein Cookie-Banner ist daher nicht erforderlich.
</p>

<h2>5. Audit-Logs</h2>
<p>
  Zur Sicherheit und Nachvollziehbarkeit werden folgende Aktionen in
  unveränderlichen Audit-Logs protokolliert: Anmeldung (erfolgreich/fehlgeschlagen),
  Registrierung, Altersverifikation, 2FA-Aktivierung/-Deaktivierung,
  Alias-Änderungen, Datenexport, Kontolöschung und AGB-Zustimmungen.
  IP-Adressen werden dabei anonymisiert.
</p>

<h2>6. Geräte-Fingerprinting und Missbrauchsschutz</h2>
<p>
  Zum Schutz der Plattform vor Mehrfach-Accounts und Manipulation setzen wir
  ein datenschutzkonformes Fingerprinting-Verfahren ein. Dabei gelten folgende
  Grundsätze:
</p>
<ul>
  <li><strong>Datenminimierung:</strong> Aus allgemeinen Geräteeigenschaften (User-Agent,
    Bildschirmauflösung, Zeitzone, Sprache, Betriebssystem-Plattform) wird
    client-seitig im Browser ein SHA-256-Hash berechnet. Nur dieser Hash wird
    an den Server übermittelt. Die Rohdaten verlassen den Browser nicht und
    werden zu keinem Zeitpunkt in unserer Datenbank gespeichert. Zusätzlich
    wird die anonymisierte IP-Adresse (letztes Oktett durch „xxx" ersetzt)
    gespeichert, um Haushalts-Gruppierungen zu ermöglichen.</li>
  <li><strong>Keine Rückberechnung:</strong> Aus dem gespeicherten Hash können
    keine Rückschlüsse auf die einzelnen Geräteeigenschaften gezogen werden.</li>
  <li><strong>Kein Tracking:</strong> Der Fingerprint-Hash dient ausschließlich
    der Erkennung von Mehrfach-Accounts und wird nicht für Werbe-,
    Analyse- oder Tracking-Zwecke verwendet.</li>
  <li><strong>Verifizierungs-Eskalation statt Sperrung:</strong> Bei Auffälligkeiten
    (z.B. viele Accounts mit gleicher IP-Adresse) erfolgt keine automatische
    Sperrung. Stattdessen wird eine zusätzliche Verifizierung angefordert
    (z.B. E-Mail-Bestätigungscode), um legitime Nutzung (z.B. Familien, Uni-WLANs)
    nicht zu beeinträchtigen.</li>
</ul>
<p>
  Rechtsgrundlage: Art. 6 Abs. 1 lit. f DSGVO (berechtigtes Interesse an
  der Sicherheit und Integrität der Plattform). Sie können der Verarbeitung
  jederzeit widersprechen (siehe Abschnitt 8).
</p>

<h2>7. Virtuelle Wallet und Spielmodi</h2>
<p>
  Im Rahmen der Squad-Spielmodi (Bankroll, Survivor, Over/Under, Fantasy,
  Kombi-Joker) werden zusätzliche Daten verarbeitet:
</p>
<ul>
  <li><strong>Wallet-Transaktionen:</strong> Jede Coin-Bewegung (Einsatz, Gewinn,
    Tagesbonus, Rückerstattung) wird in einem unveränderlichen Transaktionsprotokoll
    erfasst. Diese Daten dienen der Nachvollziehbarkeit und Integrität des
    virtuellen Wirtschaftssystems.</li>
  <li><strong>Spielmodus-Daten:</strong> Je nach gewähltem Squad-Modus werden
    Ihre Spielentscheidungen (Wetten, Picks, Vorhersagen) mit den zugehörigen
    Quoten, Einsätzen und Ergebnissen gespeichert.</li>
  <li><strong>Kein realer Geldwert:</strong> Alle Wallet-Daten beziehen sich
    ausschließlich auf virtuelle Quotico-Coins ohne realen Gegenwert.
    Es besteht kein Anspruch auf Auszahlung.</li>
</ul>
<p>
  Rechtsgrundlage: Art. 6 Abs. 1 lit. b DSGVO (Vertragserfüllung —
  Bereitstellung der Spielmodus-Funktionen).
</p>

<h2>8. Ihre Rechte</h2>
<ul>
  <li><strong>Auskunft</strong> (Art. 15 DSGVO) — Sie können jederzeit Auskunft über Ihre gespeicherten Daten verlangen.</li>
  <li><strong>Berichtigung</strong> (Art. 16 DSGVO) — Sie können die Berichtigung unrichtiger Daten verlangen.</li>
  <li><strong>Löschung</strong> (Art. 17 DSGVO) — Sie können die Löschung Ihres Kontos und Ihrer Daten verlangen. In Ihren Einstellungen steht Ihnen dafür eine Selbstbedienungsfunktion zur Verfügung.</li>
  <li><strong>Datenübertragbarkeit</strong> (Art. 20 DSGVO) — Sie können einen Export Ihrer Daten im JSON-Format über die Einstellungsseite anfordern.</li>
  <li><strong>Widerspruch</strong> (Art. 21 DSGVO) — Sie können der Verarbeitung Ihrer Daten jederzeit widersprechen.</li>
  <li><strong>Beschwerde</strong> — Sie haben das Recht, sich bei einer Aufsichtsbehörde zu beschweren.</li>
</ul>

<h2>9. Selbstbedienungsfunktionen</h2>
<p>
  Unter <strong>Einstellungen</strong> können Sie:
</p>
<ul>
  <li>Ihre persönlichen Daten als JSON exportieren — einschließlich Profil, Tipps, Wallet-Daten, Spielmodus-Aktivitäten und Fingerprint-Hashes (Datenportabilität)</li>
  <li>Ihr Sicherheitsprotokoll einsehen (letzte 50 Ereignisse)</li>
  <li>Ihr Konto löschen — Profildaten werden anonymisiert, Fingerprints vollständig gelöscht, Spielmodus-Daten anonymisiert aufbewahrt (Art. 17 DSGVO)</li>
</ul>

<h2>10. Datenweitergabe an Dritte</h2>
<p>
  Personenbezogene Daten werden nicht an Dritte weitergegeben, verkauft oder
  zu Werbezwecken genutzt. Die einzige externe Datenübermittlung erfolgt bei
  Nutzung der Google-Anmeldung (OAuth 2.0) direkt zwischen Ihrem Browser und Google.
</p>

<h2>11. Datensicherheit</h2>
<p>
  Wir setzen technische und organisatorische Maßnahmen zum Schutz Ihrer
  Daten ein: TLS-Verschlüsselung, Argon2-Passwort-Hashing, Fernet-Verschlüsselung
  für 2FA-Geheimnisse (mit Schlüsselrotation), Token-Familien zur Replay-Erkennung,
  CSRF-Schutz und Content-Security-Policy-Header.
</p>

<h2>12. Aufbewahrungsfristen</h2>
<ul>
  <li>Kontodaten: Bis zur Löschung durch den Nutzer</li>
  <li>Audit-Logs: Unbefristet (anonymisierte IP-Adressen)</li>
  <li>Server-Logs: 7 Tage</li>
  <li>Tipps nach Kontolöschung: Anonymisiert aufbewahrt (Plattform-Integrität)</li>
  <li>Wallet-Transaktionen: Bis zum Ende der Saison, danach anonymisiert. Bei Kontolöschung sofort anonymisiert.</li>
  <li>Spielmodus-Daten (Bets, Picks, Parlays): Anonymisiert aufbewahrt nach Kontolöschung (Leaderboard-Integrität)</li>
  <li>Geräte-Fingerprint-Hashes: Bis zur Kontolöschung, dann vollständig gelöscht</li>
</ul>

<h2>13. Änderungen</h2>
<p>
  Diese Datenschutzerklärung kann bei Bedarf aktualisiert werden. Die aktuelle
  Version ist stets unter <a href="/legal/datenschutz">/legal/datenschutz</a> abrufbar.
</p>
""",
    },
    "terms": {
        "title": "Allgemeine Geschäftsbedingungen (AGB)",
        "slug": "agb",
        "content_html": """
<h2>§ 1 Geltungsbereich</h2>
<p>
  Diese Allgemeinen Geschäftsbedingungen gelten für die Nutzung der Plattform
  Quotico.de (nachfolgend „Plattform"). Mit der Registrierung akzeptiert der
  Nutzer diese AGB in der jeweils gültigen Fassung.
</p>

<h2>§ 2 Leistungsbeschreibung</h2>
<p>
  Quotico.de ist ein kostenloses Tipspiel zur Unterhaltung. Nutzer geben
  Vorhersagen zu Fußball-Ergebnissen ab und sammeln dabei virtuelle Punkte.
  Innerhalb von Squads stehen verschiedene Spielmodi zur Verfügung:
</p>
<ul>
  <li><strong>Classic:</strong> Exakte Ergebnis-Vorhersagen mit Punktevergabe (0–3 Punkte pro Spiel).</li>
  <li><strong>Bankroll:</strong> Virtuelle Coins (Quotico-Coins) werden auf Spielausgänge gesetzt. Jeder Nutzer startet mit einem virtuellen Guthaben pro Saison.</li>
  <li><strong>Survivor:</strong> Pro Spieltag wird ein gewinnendes Team gewählt. Bei Fehlprognose scheidet der Nutzer aus.</li>
  <li><strong>Über/Unter:</strong> Vorhersage, ob die Gesamtanzahl der Tore über oder unter einer Linie liegt.</li>
  <li><strong>Fantasy:</strong> Teamauswahl mit Punktevergabe basierend auf realer Spielperformance (Tore, Clean Sheets).</li>
  <li><strong>Kombi-Joker (Parlay):</strong> Drei Vorhersagen werden zu einer Kombiwette verbunden.</li>
</ul>
<p>Für alle Spielmodi gilt:</p>
<ul>
  <li>Es wird <strong>kein echtes Geld</strong> eingesetzt oder ausgezahlt.</li>
  <li>Virtuelle Punkte und Quotico-Coins haben <strong>keinen monetären Wert</strong> und begründen keinen rechtlichen Anspruch auf Auszahlung oder Gewinne.</li>
  <li>Die Plattform stellt <strong>kein Glücksspiel</strong> im Sinne des Glücksspielstaatsvertrags (GlüStV) dar.</li>
  <li>Die Nutzung der virtuellen Wallet-Funktionen setzt die Bestätigung des <strong>Coin-Disclaimers</strong> voraus (einmalige Bestätigung, dass Quotico-Coins keinen realen Gegenwert haben).</li>
</ul>

<h2>§ 3 Registrierung und Nutzerkonto</h2>
<ol>
  <li>Die Registrierung ist nur für natürliche Personen ab 18 Jahren gestattet.</li>
  <li>Jede Person darf nur <strong>ein einziges Konto</strong> betreiben. Das Erstellen mehrerer Konten (Multi-Accounting) ist untersagt und kann zur Sperrung führen.</li>
  <li>Der Nutzer ist für die Sicherheit seiner Zugangsdaten (Passwort, 2FA) selbst verantwortlich.</li>
  <li>Anmeldedaten dürfen nicht an Dritte weitergegeben werden.</li>
  <li>Zur Erkennung von Mehrfach-Accounts wird ein datenschutzkonformer Geräte-Fingerprint (SHA-256-Hash) erhoben. Einzelheiten hierzu finden Sie in der <a href="/legal/datenschutz">Datenschutzerklärung</a> (Abschnitt 6).</li>
</ol>

<h2>§ 4 Verhaltensregeln</h2>
<p>Es ist untersagt:</p>
<ul>
  <li>Bots, Skripte oder automatisierte Werkzeuge zur Abgabe von Tipps oder zur Manipulation des Dienstes einzusetzen.</li>
  <li>Die Plattform in einer Weise zu nutzen, die den Betrieb oder andere Nutzer beeinträchtigt.</li>
  <li>Sicherheitsmechanismen zu umgehen oder auszunutzen.</li>
  <li>Falsche Angaben bei der Registrierung zu machen (insbesondere zum Alter).</li>
</ul>

<h2>§ 5 Sperrung und Kündigung</h2>
<ol>
  <li>Der Betreiber kann Nutzerkonten bei Verstoß gegen diese AGB vorübergehend oder dauerhaft sperren.</li>
  <li>Der Nutzer kann sein Konto jederzeit in den Einstellungen löschen (Art. 17 DSGVO). Die Löschung erfolgt durch Anonymisierung.</li>
  <li>Gesperrte oder gelöschte Nutzer haben keinen Anspruch auf Wiederherstellung von Punkten oder Daten.</li>
</ol>

<h2>§ 6 Haftung</h2>
<ol>
  <li>Der Betreiber haftet nicht für die Richtigkeit der angezeigten Quoten, Ergebnisse oder Punkteberechnungen.</li>
  <li>Der Betreiber übernimmt keine Garantie für die ständige Verfügbarkeit der Plattform.</li>
  <li>Externe Datenquellen (Quoten, Spielergebnisse) werden automatisiert abgerufen. Fehler in diesen Daten begründen keinen Anspruch.</li>
</ol>

<h2>§ 7 Datenschutz</h2>
<p>
  Es gilt die <a href="/legal/datenschutz">Datenschutzerklärung</a> in der
  jeweils aktuellen Fassung. Diese ist Bestandteil der AGB.
</p>

<h2>§ 8 Änderungen der AGB</h2>
<p>
  Der Betreiber behält sich vor, diese AGB jederzeit zu ändern. Nutzer werden
  bei der nächsten Anmeldung aufgefordert, die geänderten AGB zu akzeptieren.
  Die Nutzung der Plattform setzt die Zustimmung zur jeweils gültigen Fassung voraus.
</p>

<h2>§ 9 Anwendbares Recht und Gerichtsstand</h2>
<p>
  Es gilt das Recht der Bundesrepublik Deutschland unter Ausschluss des
  UN-Kaufrechts. Gerichtsstand ist, soweit gesetzlich zulässig, der Sitz
  des Betreibers.
</p>
""",
    },
    "youth-protection": {
        "title": "Jugendschutz",
        "slug": "jugendschutz",
        "content_html": """
<h2>Jugendschutzkonzept gemäß JuSchG / JMStV</h2>

<h3>1. Einordnung des Angebots</h3>
<p>
  Quotico.de ist ein <strong>Tipspiel zur Unterhaltung</strong> (Simulation).
  Es wird kein echtes Geld eingesetzt und es erfolgen keine Auszahlungen.
  Die Plattform stellt kein Glücksspiel im Sinne des Glücksspielstaatsvertrags
  (GlüStV 2021) dar, da die wesentlichen Merkmale — Entgelt, Zufallsabhängigkeit
  mit Vermögenswert — nicht erfüllt sind.
</p>
<p>
  Dennoch simuliert Quotico.de den Ablauf einer Sportwette (Quotenauswahl,
  Tippabgabe, Punktevergabe basierend auf Quoten). Um einen verantwortungsvollen
  Umgang sicherzustellen, beschränken wir die Nutzung auf volljährige Personen.
</p>

<h3>2. Altersbeschränkung</h3>
<p>
  <strong>Die Teilnahme an Quotico.de ist erst ab 18 Jahren gestattet.</strong>
</p>
<ul>
  <li>Bei der Registrierung wird das Geburtsdatum abgefragt und serverseitig validiert.</li>
  <li>Nutzer unter 18 Jahren werden von der Registrierung ausgeschlossen.</li>
  <li>Bei Anmeldung über Google OAuth wird die Altersverifikation nachgeholt (Profilvervollständigung).</li>
  <li>Jede Altersverifikation wird in einem Audit-Log protokolliert.</li>
</ul>

<h3>3. Technische Schutzmaßnahmen</h3>
<ul>
  <li><strong>Altersabfrage bei Registrierung:</strong> Geburtsdatum-Eingabe mit serverseitiger Prüfung (≥ 18 Jahre).</li>
  <li><strong>Altersbestätigung für Besucher:</strong> Nicht-angemeldete Besucher werden beim ersten Besuch zur Bestätigung ihrer Volljährigkeit aufgefordert.</li>
  <li><strong>Wallet-Alterssperre:</strong> Alle Wallet- und Coin-Funktionen (Bankroll-Modus, Über/Unter mit Coins) sind zusätzlich hinter einer Altersprüfung gesperrt (<code>is_adult</code>). Nicht-verifizierte Nutzer erhalten den HTTP-Status 403 „Altersprüfung erforderlich".</li>
  <li><strong>Coin-Disclaimer:</strong> Vor der ersten Nutzung der virtuellen Wallet muss der Nutzer einmalig bestätigen, dass Quotico-Coins keinen realen Gegenwert haben.</li>
  <li><strong>Verantwortungsvolles Spielen:</strong> Bei Einsätzen über 50% des virtuellen Guthabens wird eine Warnmeldung angezeigt.</li>
  <li><strong>Progressiver Tagesbonus:</strong> Bei virtuellem Bankrott wird kein sofortiger Refill gewährt. Stattdessen erhalten Nutzer über 3 Tage hinweg progressive Tagesboni (50/100/150 Coins), um impulsives „Chasing"-Verhalten zu vermeiden.</li>
  <li><strong>Hinweistexte:</strong> Auf jeder Seite ist der Hinweis „Ab 18 Jahren. Kein echtes Geld." sichtbar.</li>
  <li><strong>Kein Echtgeld:</strong> Die Plattform bietet keine Zahlungsfunktionen. Es gibt keine Einzahlungen, Auszahlungen oder In-App-Käufe.</li>
</ul>

<h3>4. Kennzeichnung</h3>
<p>
  Die Plattform enthält folgende Hinweise:
</p>
<ul>
  <li>Im Seitenfuß: „Ab 18 Jahren. Kein echtes Geld. Quotico dient nur der Unterhaltung."</li>
  <li>In der Kopfzeile: „Tipspiel — kein Echtgeld"</li>
  <li>Bei der Registrierung: Pflicht-Checkbox mit Bestätigung der Volljährigkeit</li>
</ul>

<h3>5. Ansprechpartner Jugendschutz</h3>
<p>
  Für Fragen zum Jugendschutz wenden Sie sich bitte an:<br>
  <a href="mailto:kontakt@quotico.de">kontakt@quotico.de</a>
</p>
""",
    },
}

# Reverse lookup: slug -> document key
SLUG_TO_KEY = {doc["slug"]: key for key, doc in LEGAL_DOCS.items()}
