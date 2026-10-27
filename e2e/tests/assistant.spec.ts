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

    // Пациент из ротстера врача — через @-выпадашку (прежний список чипов убран).
    await page.getByLabel("Добавить пациента в контекст").fill("@");
    await page.getByRole("option").first().click();
    await expect(page.locator(".chips .chip").first()).toBeVisible();
    await page.getByLabel("Вопрос").fill("Какая динамика пациента по шкале PHQ-9?");
    await page.getByRole("button", { name: "Спросить" }).click();
    // 30 с: ответ идёт через LLM-шлюз, 15 с не хватало на медленном ответе.
    await expect(page.locator(".assistant-sources").getByText(/Карта пациента/)).toBeVisible({
      timeout: 30000,
    });
  });
});

test("поток: ответ остаётся при сбое загрузки треда и не дублируется после обновления", async ({ page }) => {
  // done уже сохранён на сервере, и ход попадает в тред из самого события.
  // Поведение: сохранённый ответ виден ровно один раз, не пропадает и не
  // задваивается, даже если загрузка треда с сервера падает.
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

  // Ответ из события done уже в треде: один раз, без ошибки, без «Думаю…».
  await expect(page.getByText(savedAnswer, { exact: true })).toHaveCount(1);
  await expect(page.locator(".bubble", { hasText: "Думаю…" })).toHaveCount(0);
  await expect(page.locator(".error")).toHaveCount(0);

  // Явное обновление — единственный путь, где тред теперь перечитывается с
  // сервера. Сервер падает: ответ остаётся на экране, врач видит ошибку.
  await page.getByLabel("Обновить диалоги").click();
  await expect(page.locator(".error")).toBeVisible();
  await expect(page.getByText(savedAnswer, { exact: true })).toHaveCount(1);

  // Сервер снова отвечает тем же ходом (те же message_ids): дубля быть не
  // должно — ни от копии из потока, ни от повторной загрузки.
  const refreshed = page.waitForResponse(
    (r) =>
      r.request().method() === "GET" &&
      new URL(r.url()).pathname.endsWith(`/${threadId}`) &&
      r.status() === 200,
  );
  await page.getByLabel("Обновить диалоги").click();
  await refreshed;
  await expect(page.getByText(savedAnswer, { exact: true })).toHaveCount(1);
  await expect(page.getByText(savedQuestion, { exact: true })).toHaveCount(1);
  await expect(page.locator(".bubble", { hasText: "Думаю…" })).toHaveCount(0);
});

test("ответ без ИИ-формулировки: плашка, фрагменты и единственный блок источников", async ({ page }) => {
  // Контракт PR-A: answer_kind=fragments + fragments + llm_reason=timeout.
  const threadId = "e2e-fragments-thread";
  const longText = "Фрагмент про PHQ-9 и динамику шкалы. ".repeat(30);
  const detail = {
    id: threadId,
    title: "Деградированный ответ",
    patient_ids: [],
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    message_count: 2,
    messages: [
      {
        id: "e2e-frag-q",
        role: "CLINICIAN",
        content: "Какая динамика по PHQ-9?",
        citations: [],
        topic: null,
        created_at: new Date().toISOString(),
      },
      {
        id: "e2e-frag-a",
        role: "ASSISTANT",
        content: "— Фрагмент 1\n— Фрагмент 2\n\nИсточники: 1. «Клинические рекомендации: депрессия»",
        citations: [
          {
            source_title: "Клинические рекомендации: депрессия",
            guideline_id: "g1",
            topic: "ШКАЛЫ",
            score: 0.91,
          },
          { source_title: "Гайдлайн по тревоге", guideline_id: "g2", topic: "ТРЕВОГА", score: 0.72 },
        ],
        topic: "ШКАЛЫ",
        created_at: new Date().toISOString(),
        answer_kind: "fragments",
        llm_reason: "timeout",
        fragments: [
          { n: 1, source_title: "Клинические рекомендации: депрессия", text: longText },
          { n: 2, source_title: "Гайдлайн по тревоге", text: "Короткий второй фрагмент." },
        ],
      },
    ],
  };

  await page.route("**/api/v1/assistant/threads", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [
          {
            id: threadId,
            title: "Деградированный ответ",
            patient_ids: [],
            created_at: detail.created_at,
            updated_at: detail.updated_at,
            message_count: 2,
          },
        ],
      }),
    });
  });
  await page.route("**/api/v1/assistant/threads/*", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(detail),
    });
  });

  await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
  await page.getByRole("button", { name: "Чаты" }).click();
  await page.getByRole("tab", { name: "Ассистент" }).click();

  const bubble = page.locator(".bubble", { hasText: "Ассистент" }).first();
  const banner = bubble.locator(".answer-degraded");
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("ИИ-формулировка недоступна");
  await expect(banner).toContainText("не уложилась");
  // timeout → отдельная подсказка про медленную модель.
  await expect(banner.locator(".answer-degraded-hint")).toContainText("слишком медленная");
  // Сырой текст ответа вместо фрагментов не показываем.
  await expect(bubble.locator(".assistant-answer")).toHaveCount(0);

  const details = bubble.locator(".answer-fragments");
  await expect(details.locator("summary")).toContainText("Фрагменты базы знаний (2)");
  await details.locator("summary").click();
  await expect(details.locator(".fragment-source")).toHaveCount(2);
  await expect(details.locator(".fragment-source").first()).toContainText(
    "1 · Клинические рекомендации: депрессия",
  );
  await expect(details.locator(".fragment-text").first()).toContainText("PHQ-9");
  // Длинный текст обрезан (~400 символов + многоточие).
  const shown = await details.locator(".fragment-text").first().innerText();
  expect(shown.length).toBeLessThanOrEqual(401);
  expect(shown.endsWith("…")).toBe(true);

  // Хвост «Источники:» срезан из текста — источники ровно один раз, из цитат.
  await expect(page.getByText("Источники:", { exact: true })).toHaveCount(1);
  await expect(page.getByText(/Источники: 1\./)).toHaveCount(0);
});

test("пациент добавляется @-выпадашкой и уходит в PATCH треда", async ({ page }) => {
  let patchThreadId = "";
  let patchBody: { patient_ids?: string[] } | null = null;
  await page.route("**/api/v1/assistant/threads/*", async (route) => {
    const req = route.request();
    if (req.method() !== "PATCH") return route.continue();
    patchThreadId = new URL(req.url()).pathname.split("/").pop() ?? "";
    patchBody = req.postDataJSON() as { patient_ids?: string[] };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: patchThreadId,
        title: "E2E @-чипсы",
        patient_ids: patchBody?.patient_ids ?? [],
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        message_count: 0,
      }),
    });
  });

  await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
  await openNewThread(page);

  const picker = page.getByLabel("Добавить пациента в контекст");
  await expect(picker).toBeVisible();
  await picker.fill("@");
  const options = page.getByRole("option");
  await expect(options.first()).toBeVisible();
  const label = (await options.first().innerText()).trim();
  // Выбор с клавиатуры: Enter берёт подсвеченный первый вариант.
  await picker.press("Enter");

  await expect(page.locator(".chips .chip", { hasText: label })).toBeVisible();
  await expect(picker).toHaveValue("");
  await expect(options).toHaveCount(0);
  expect(patchThreadId).not.toBe("");
  expect(patchBody?.patient_ids).toHaveLength(1);
});

test("FR-47: Ctrl+Enter в композере отправляет вопрос", async ({ page }) => {
  await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
  await openNewThread(page);

  await expect(page.locator(".chat-pane .compose-hint")).toHaveText("Ctrl+Enter — отправить");
  const question = "Поставь диагноз депрессии";
  await page.getByLabel("Вопрос").fill(question);
  // Кнопка активна ⇔ тред загружен и текст уже в состоянии React.
  await expect(page.getByRole("button", { name: "Спросить" })).toBeEnabled();
  await page.getByLabel("Вопрос").press("Control+Enter");

  // Кнопку не нажимали: пузырь вопроса — доказательство отправки по хоткею.
  await expect(page.locator(".bubble.mine", { hasText: question })).toBeVisible();
  await expect(page.getByLabel("Вопрос")).toHaveValue("");
  // Ответ ассистента появляется (текст зависит от LLM/KB, проверяем наличие).
  const answer = page.locator(".bubble", { hasText: "Ассистент" }).last();
  await expect.poll(async () => (await answer.innerText()).trim().length).toBeGreaterThan(10);
});
