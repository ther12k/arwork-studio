import { expect, test } from "@playwright/test";
import { INK, mkState, routeBackend } from "../browser/studio-fixture";

test("debug ink style pixels", async ({ page }) => {
  const state = mkState();
  await routeBackend(page, state);
  await page.goto("/");
  await page.getByRole("button", { name: "Edit regions" }).click();
  await page.getByRole("button", { name: "Art node" }).click();
  const dump = (label: string) =>
    page.evaluate(() => {
      const board = document.querySelector('svg[aria-label*="interactive coloring artwork"]');
      const imgs = [...(board?.querySelectorAll("image") ?? [])].map((i) => i.id);
      const img = board?.querySelector('image[id$="underpaint-ink"]') as SVGImageElement | null;
      const href = img?.getAttribute("href");
      if (!href) return { imgs, href: null };
      return new Promise((resolve) => {
        const image = new Image();
        image.onload = () => {
          const canvas = document.createElement("canvas");
          canvas.width = 300;
          canvas.height = 300;
          const ctx = canvas.getContext("2d")!;
          ctx.drawImage(image, 0, 0, 300, 300);
          const out: number[][] = [];
          for (let dx = -4; dx <= 4; dx += 2)
            for (let dy = -4; dy <= 4; dy += 2)
              out.push([...ctx.getImageData(120 + dx, 49 + dy, 1, 1).data]);
          resolve({ imgs, pixels: out });
        };
        image.onerror = (e) => resolve({ imgs, err: String(e) });
        image.src = href;
      });
    }).then((r) => console.log(label, JSON.stringify(r)));
  await page.waitForTimeout(1500);
  await dump("BEFORE");
  await page.locator('input[aria-label="Stroke color"]').fill("#29383E");
  await page.locator('input[aria-label="Stroke opacity"]').fill("50");
  await page.getByRole("button", { name: "Save appearance" }).click();
  await page.waitForTimeout(5000);
  await dump("AFTER");
  expect(true).toBe(true);
}, 60_000);
