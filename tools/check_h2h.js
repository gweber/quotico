// mongosh script: check H2H resolution for all upcoming matches
// Usage: mongosh mongodb://localhost:27017/quotico tools/check_h2h.js

const matches = db.matches.find(
  { status: { $in: ["upcoming", "live"] } },
  { sport_key: 1, teams: 1 }
).sort({ commence_time: 1 }).toArray();

const aliases = db.team_aliases.find({}).toArray();

// Build lookup: sport_key|team_name -> team_key
const byName = {};
aliases.forEach(a => { byName[a.sport_key + "|" + a.team_name] = a.team_key; });

// Build lookup: sport_key|team_key -> team_key
const byKey = {};
aliases.forEach(a => { byKey[a.sport_key + "|" + a.team_key] = a.team_key; });

// Noise words
const NOISE = new Set(["fc","cf","sc","ac","as","ss","us","afc","rcd","1.","club","de","sv","vfb","vfl","tsg","fsv","bsc","fk"]);

function teamNameKey(name) {
  name = name.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").trim().toLowerCase();
  const tokens = name.split(/\s+/).filter(function(t) { return !NOISE.has(t) && t.length >= 3; }).sort();
  return tokens.join(" ");
}

// Related sport keys
const RELATED = {
  "soccer_germany_bundesliga": ["soccer_germany_bundesliga", "soccer_germany_bundesliga2"],
  "soccer_germany_bundesliga2": ["soccer_germany_bundesliga", "soccer_germany_bundesliga2"],
};

function resolve(teamName, sportKey) {
  const sports = RELATED[sportKey] || [sportKey];

  // 1. Exact name
  for (const sk of sports) {
    const r = byName[sk + "|" + teamName];
    if (r) return { key: r, via: "exact-name" };
  }

  // 2. Exact key
  const computed = teamNameKey(teamName);
  for (const sk of sports) {
    const r = byKey[sk + "|" + computed];
    if (r) return { key: r, via: "exact-key" };
  }

  // 3. Containment (last token)
  const lastToken = computed.split(" ").pop();
  const candidates = aliases.filter(function(a) {
    return sports.includes(a.sport_key) && a.team_key.includes(lastToken);
  });
  for (const c of candidates) {
    const stored = new Set(c.team_key.split(" "));
    const comp = new Set(computed.split(" "));
    const storedSub = [...stored].every(function(t) { return comp.has(t); });
    const compSub = [...comp].every(function(t) { return stored.has(t); });
    if (storedSub || compSub) return { key: c.team_key, via: "containment" };
  }

  // 4. Longest token
  const tokens = computed.split(" ");
  const longest = tokens.reduce(function(a, b) { return a.length >= b.length ? a : b; }, "");
  if (longest.length >= 4) {
    const found = aliases.find(function(a) {
      return sports.includes(a.sport_key) && a.team_key.includes(longest);
    });
    if (found) return { key: found.team_key, via: "longest-token" };
  }

  // 5. Suffix match
  const computedAlpha = computed.replace(/[^a-z]/g, "");
  let bestMatch = null;
  let bestLen = 0;
  aliases.filter(function(a) { return sports.includes(a.sport_key); }).forEach(function(a) {
    const storedAlpha = a.team_key.replace(/[^a-z]/g, "");
    const maxCheck = Math.min(computedAlpha.length, storedAlpha.length);
    let suffixLen = 0;
    for (let i = 1; i <= maxCheck; i++) {
      if (computedAlpha[computedAlpha.length - i] === storedAlpha[storedAlpha.length - i]) {
        suffixLen = i;
      } else {
        break;
      }
    }
    if (suffixLen >= 7 && suffixLen > bestLen) {
      bestLen = suffixLen;
      bestMatch = a;
    }
  });
  if (bestMatch) return { key: bestMatch.team_key, via: "suffix(" + bestLen + ")" };

  return null;
}

// Check all matches
const failed = [];
const succeeded = {};

matches.forEach(function(m) {
  const sk = m.sport_key;
  const homeR = resolve(m.teams.home, sk);
  const awayR = resolve(m.teams.away, sk);

  if (!homeR || !awayR) {
    failed.push({
      sport: sk,
      home: m.teams.home,
      away: m.teams.away,
      homeResult: homeR ? homeR.key + " (" + homeR.via + ")" : "FAIL [key=" + teamNameKey(m.teams.home) + "]",
      awayResult: awayR ? awayR.key + " (" + awayR.via + ")" : "FAIL [key=" + teamNameKey(m.teams.away) + "]",
    });
  } else {
    succeeded[sk] = (succeeded[sk] || 0) + 1;
  }
});

print("=== RESOLVED ===");
Object.keys(succeeded).sort().forEach(function(sk) {
  print("  " + sk + ": " + succeeded[sk] + " matches OK");
});

print("\n=== FAILED (" + failed.length + ") ===");
failed.forEach(function(f) {
  print("\n  " + f.sport + ": " + f.home + " vs " + f.away);
  if (f.homeResult.startsWith("FAIL")) print("    HOME: " + f.homeResult);
  if (f.awayResult.startsWith("FAIL")) print("    AWAY: " + f.awayResult);
});
