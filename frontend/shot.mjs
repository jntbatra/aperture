import { chromium } from 'playwright'
const OUT = process.argv[2]
const QUESTION = process.argv[3] || ''
const browser = await chromium.launch({ channel: 'chromium' })
const page = await browser.newPage({ viewport: { width: 1340, height: 940 }, deviceScaleFactor: 2 })
const errors = []
page.on('console', (m) => m.type() === 'error' && errors.push(m.text()))
page.on('pageerror', (e) => errors.push(String(e)))
await page.goto('http://localhost:8000', { waitUntil: 'networkidle' })
await page.waitForTimeout(800)
if (QUESTION) {
  await page.fill('textarea', QUESTION)
  await page.keyboard.press('Enter')
  await page.waitForFunction(() => document.body.innerText.includes('See the SQL'), { timeout: 150000 })
  await page.waitForTimeout(1200)
}
await page.screenshot({ path: OUT, fullPage: false })
console.log('shot:', OUT, '| console errors:', errors.length ? errors.slice(0, 3) : 'none')
await browser.close()
