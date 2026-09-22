import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

/**
 * The dev server proxies /api to the backend, so the browser only ever talks
 * to one origin.
 *
 * Why this matters more than it looks
 * -----------------------------------
 * The session is a cookie with `SameSite=Lax`, which is not sent on
 * cross-site subrequests. With the frontend on one origin and the API on
 * another, sign-up succeeds, the cookie is stored, and every request after it
 * arrives unauthenticated — an interface that looks exactly like a broken
 * login and is nothing of the sort.
 *
 * It bit immediately: the page opened on `127.0.0.1:5173` while the client
 * pointed at `localhost:8000`. Same machine, different hosts, therefore
 * cross-site, therefore no cookie. Relaxing SameSite to `None` would have
 * "fixed" it by turning the session into a cookie any site can cause to be
 * sent, which is the trade CSRF exists because of.
 *
 * Proxying removes the problem rather than working around it, and it matches
 * production — where the frontend and the API sit behind one domain — so the
 * thing developed locally is the thing deployed.
 */
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
        // Server-Sent Events: without this the progress stream is buffered by
        // the proxy and the UI shows nothing until the answer is complete,
        // which defeats the point of streaming it.
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            if (proxyRes.headers['content-type']?.includes('text/event-stream')) {
              proxyRes.headers['cache-control'] = 'no-cache, no-transform'
            }
          })
        },
      },
    },
  },
})
