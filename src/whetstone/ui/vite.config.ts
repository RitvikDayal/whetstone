import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The build the Python wheel ships. `hatch_build.py` runs `npm run build` and
// `pyproject.toml`'s `[tool.hatch.build] artifacts` puts `dist/**` into the
// wheel and the sdist -- without that stanza `.gitignore`'s unanchored `dist/`
// makes hatchling drop every file here, which was measured.
export default defineConfig({
  plugins: [react()],
  // Relative, so the bundle does not assume it is mounted at the site root.
  base: './',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // NO INLINE SCRIPT. Vite's modulePreload polyfill is injected as an inline
    // <script>, and the server serves
    //   Content-Security-Policy: ... script-src 'self'
    // with no 'unsafe-inline'. Leaving the polyfill on would mean either a
    // blank page or a CSP relaxed to permit inline script -- and this page
    // renders model-authored strings read out of a repository the user did not
    // necessarily write, so 'unsafe-inline' here is not a small concession.
    // Every browser this targets supports modulepreload natively.
    modulePreload: { polyfill: false },
    // Assets are same-origin and short-lived; no CDN, no remote fonts, no
    // remote images -- the same rule report/html.py already holds itself to.
    assetsInlineLimit: 0,
    sourcemap: false,
  },
})
