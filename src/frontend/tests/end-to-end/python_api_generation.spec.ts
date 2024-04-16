import { expect, test } from "@playwright/test";

test("python_api_generation", async ({ page, context }) => {
  await page.goto("/");
  let modalCount = (await page?.getByTestId("modal-title")?.count()) ?? 0;

  while (modalCount === 0) {
    await page.locator('//*[@id="new-project-btn"]').click();
    await page.waitForTimeout(5000);
    modalCount = await page.getByTestId("modal-title")?.count();
  }
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.getByRole("heading", { name: "Basic Prompting" }).click();
  await page.waitForTimeout(2000);
  await page.getByText("API", { exact: true }).click();
  await page.getByRole("tab", { name: "Python API" }).click();
  await page.getByRole("button", { name: "Copy Code" }).click();
  const handle = await page.evaluateHandle(() =>
    navigator.clipboard.readText()
  );
  const clipboardContent = await handle.jsonValue();
  expect(clipboardContent.length).toBeGreaterThan(0);
});
