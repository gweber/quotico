<template>
  <footer class="bg-surface-1 border-t border-surface-3 mt-auto">
    <div class="max-w-screen-2xl mx-auto px-4 py-4 flex flex-col sm:flex-row items-center justify-between gap-2">
      <p class="text-xs text-text-muted text-center sm:text-left">
        Ab 18 Jahren. Kein echtes Geld. Quotico dient nur der Unterhaltung.
      </p>
      <nav class="flex items-center gap-4" aria-label="Rechtliche Links">
        <RouterLink
          to="/legal/impressum"
          class="text-xs text-text-muted hover:text-text-secondary transition-colors"
        >
          Impressum
        </RouterLink>
        <RouterLink
          to="/legal/datenschutz"
          class="text-xs text-text-muted hover:text-text-secondary transition-colors"
        >
          Datenschutz
        </RouterLink>
        <RouterLink
          to="/legal/agb"
          class="text-xs text-text-muted hover:text-text-secondary transition-colors"
        >
          AGB
        </RouterLink>
        <RouterLink
          to="/legal/jugendschutz"
          class="text-xs text-text-muted hover:text-text-secondary transition-colors"
        >
          Jugendschutz
        </RouterLink>
      </nav>
    </div>
  </footer>
</template>
