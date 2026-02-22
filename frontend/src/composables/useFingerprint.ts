/**
 * DSGVO-compliant device fingerprinting.
 *
 * Only computes a SHA-256 hash client-side — raw device properties
 * never leave the browser and are never stored anywhere.
 */

let cachedHash: string | null = null;

export async function collectFingerprint(): Promise<string> {
  if (cachedHash) return cachedHash;

  const parts = [
    navigator.userAgent,
    `${screen.width}x${screen.height}`,
    Intl.DateTimeFormat().resolvedOptions().timeZone,
    navigator.language,
    navigator.platform,
  ];

  const data = new TextEncoder().encode(parts.join("|"));
  const hashBuffer = await crypto.subtle.digest("SHA-256", data);
  const hashArray = Array.from(new Uint8Array(hashBuffer));
  cachedHash = hashArray.map((b) => b.toString(16).padStart(2, "0")).join("");
  return cachedHash;
}

export function useFingerprint() {
  return { collectFingerprint };
}
