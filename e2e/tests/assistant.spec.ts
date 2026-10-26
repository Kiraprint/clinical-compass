// e2e вкладки «Ассистент» врача: треды с контекстом, гибридный поиск по базе
// знаний + цитаты (FR-21). UI: колонка диалогов слева, пациенты треда — чипами.
import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import { DEMO_PASSWORD, DEMO_TOTP, login } from "./helpers";

/** Открывает «Чаты → Ассистент» и заводит новый диалог (он выбирается сразу). */
async function openNewThread(page: Page) {
  await page.getByRole("button", { name: "Чаты" }).click();
  await page.getByRole("tab", { name: "Ассистент" }).click();
  await expect(page.getByRole("heading", { name: "Ассистент врача" })).toBeVisible();
  await page.locator('.icon-btn[title="Новый диалог"]').click();
  await expect(page.getByLabel("Вопрос")).toBeVisible();
}

test.describe("ИИ-ассистент врача", () => {
  test("тред отвечает с источниками по базе знаний", async ({ page }) => {
    await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
    await openNewThread(page);

    // Справочный запрос → ответ с цитатами.
    await page.getByLabel("Вопрос").fill("Какой порог тяжести у шкалы PHQ-9?");
    await page.getByRole("button", { name: "Спросить" }).click();
    await expect(page.getByText("По базе знаний найдено следующее:")).toBeVisible();
    await expect(
      page.locator(".assistant-sources").getByText("Источники:", { exact: true }),
    ).toBeVisible();

    // Запрос на диагноз в том же треде → отказ (красная линия).
    await page.getByLabel("Вопрос").fill("Поставь диагноз депрессии");
    await page.getByRole("button", { name: "Спросить" }).click();
    await expect(page.getByText("Я не могу поставить диагноз")).toBeVisible();
  });

  test("справка по препарату Авиандр — не red line, ответ с цитатой инструкции", async ({ page }) => {
    await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
    await openNewThread(page);

    await page.getByLabel("Вопрос").fill("Какая дозировка Авиандра?");
    await page.getByRole("button", { name: "Спросить" }).click();
    await expect(page.getByText("По базе знаний найдено следующее:")).toBeVisible({
      timeout: 30000,
    });
    // Цитата чанка препарата (АВИАНДР).
    await expect(page.locator(".assistant-sources").getByText(/АВИАНДР/i).first()).toBeVisible();
  });

  test("пациент в контексте треда подмешивает карту в ответ", async ({ page }) => {
    await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
    await openNewThread(page);

    // Чип пациента из ротстера врача → карта попадает в контекст треда.
    await page.getByTitle("Добавить в контекст").first().click();
    await page.getByLabel("Вопрос").fill("Какая динамика пациента по шкале PHQ-9?");
    await page.getByRole("button", { name: "Спросить" }).click();
    // 30 с: ответ идёт через LLM-шлюз, 15 с не хватало на медленном ответе.
    await expect(page.locator(".assistant-sources").getByText(/Карта пациента/)).toBeVisible({
      timeout: 30000,
    });
  });
});

test("поток: ответ остаётся при сбое refetch и не дублируется после обновления", async ({ page }) => {
  // P1 ре-ревью #98: done уже сохранён на сервере. Если refetch сразу после
  // потока упал, ход должен остаться на экране ровно один раз, в том числе
  // после повторной загрузки треда.
  const savedAnswer = "E2E поток: сохранённый ответ [1]";
  const savedQuestion = "PHQ-9: проверка потока";
  let threadId = "";
  let streamDone = false;
  let detailFails = 0;

  const detail = () => ({
    id: threadId,
    title: "Поток E2E",
    patient_ids: [],
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    message_count: 2,
    messages: [
      {
        id: "e2e-stream-q",
        role: "CLINICIAN",
        content: savedQuestion,
        citations: [],
        topic: null,
        created_at: new Date().toISOString(),
      },
      {
        id: "e2e-stream-a",
        role: "ASSISTANT",
        content: savedAnswer,
        citations: [{ source_title: "Гайдлайн", guideline_id: "g1", topic: "ШКАЛЫ", score: 0.9 }],
        topic: "ШКАЛЫ",
        created_at: new Date().toISOString(),
      },
    ],
  });

  await page.route("**/api/v1/assistant/threads/*/messages/stream", async (route) => {
    threadId = new URL(route.request().url()).pathname.split("/")[5];
    streamDone = true;
    const sse = [
      'event: meta\ndata: {"skipped_patient_ids":[],"retrieval_query":"PHQ-9","rewritten":false}\n\n',
      'event: token\ndata: {"t":"E2E поток: "}\n\n',
      `event: done\ndata: ${JSON.stringify({
        answer: savedAnswer,
        citations: [{ source_title: "Гайдлайн", guideline_id: "g1", topic: "ШКАЛЫ", score: 0.9 }],
        topic: "ШКАЛЫ",
        grounded: true,
        refused: false,
        refusal_code: null,
        skipped_patient_ids: [],
        message_ids: ["e2e-stream-q", "e2e-stream-a"],
      })}\n\n`,
    ].join("");
    await route.fulfill({
      status: 200,
      headers: {
        "content-type": "text/event-stream; charset=utf-8",
        "cache-control": "no-cache",
        "content-encoding": "identity",
      },
      body: sse,
    });
  });

  await page.route("**/api/v1/assistant/threads/*", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const isDetail = req.method() === "GET" && threadId !== "" && path.endsWith(`/${threadId}`);
    if (!isDetail) return route.continue();
    if (streamDone && detailFails === 0) {
      detailFails += 1;
      return route.abort("failed");
    }
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(detail()) });
  });

  await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
  await openNewThread(page);
  await page.getByLabel("Вопрос").fill(savedQuestion);
  await page.getByRole("button", { name: "Спросить" }).click();

  // refetch упал: ответ из done уже в треде, ошибка показана, «Думаю…» нет.
  await expect(page.getByText(savedAnswer, { exact: true })).toBeVisible();
  await expect(page.locator(".bubble", { hasText: "Думаю…" })).toHaveCount(0);
  await expect(page.locator(".error")).toBeVisible();

  // Повторная загрузка треда: сервер вернул тот же ход, дубля быть не должно.
  const refreshed = page.waitForResponse(
    (r) => r.request().method() === "GET" && new URL(r.url()).pathname.endsWith(`/${threadId}`),
  );
  await page.getByLabel("Обновить диалоги").click();
  await refreshed;
  await expect(page.getByText(savedAnswer, { exact: true })).toHaveCount(1);
  await expect(page.getByText(savedQuestion, { exact: true })).toHaveCount(1);
  await expect(page.locator(".bubble", { hasText: "Думаю…" })).toHaveCount(0);
});
