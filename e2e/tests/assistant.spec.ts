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

type StubCitation = { source_title: string; guideline_id: string; topic: string; score?: number };
type StubMessage = {
  id: string;
  role: "CLINICIAN" | "ASSISTANT";
  content: string;
  citations?: StubCitation[];
  topic?: string | null;
  answer_kind?: string;
  llm_reason?: string | null;
  fragments?: { n: number; source_title: string; text: string }[];
};

/**
 * Стаб треда: список из одного диалога и его деталь, затем вход во вкладку.
 * Поля контракта PR-A (answer_kind/llm_reason/fragments) передаются как есть —
 * отсутствие поля = ответ старого API.
 */
async function openStubbedThread(
  page: Page,
  threadId: string,
  title: string,
  messages: StubMessage[],
) {
  const now = new Date().toISOString();
  const detail = {
    id: threadId,
    title,
    patient_ids: [],
    created_at: now,
    updated_at: now,
    message_count: messages.length,
    messages: messages.map((m) => ({ citations: [], topic: null, created_at: now, ...m })),
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
            title,
            patient_ids: [],
            created_at: now,
            updated_at: now,
            message_count: messages.length,
          },
        ],
      }),
    });
  });
  await page.route("**/api/v1/assistant/threads/*", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(detail) });
  });

  await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
  await page.getByRole("button", { name: "Чаты" }).click();
  await page.getByRole("tab", { name: "Ассистент" }).click();
}

test.describe("ИИ-ассистент врача", () => {
  test("тред отвечает с источниками по базе знаний", async ({ page }) => {
    await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
    await openNewThread(page);

    // Справочный запрос → ответ с цитатами.
    await page.getByLabel("Вопрос").fill("Какой порог тяжести у шкалы PHQ-9?");
    await page.getByRole("button", { name: "Спросить" }).click();
    // С PR-A ответ может прийти двумя видами: ИИ-формулировка или
    // детерминированные фрагменты с плашкой — оба обязаны нести источники.
    await expect(
      page.getByText(/По базе знаний найдено следующее:|ИИ-формулировка недоступна/),
    ).toBeVisible();
    // Блок «Источники» свёрнут: шапка со счётчиком видна, пункты — после клика.
    const sources = page.locator(".assistant-sources").first();
    await expect(sources.locator("summary")).toHaveText(/^Источники \(\d+\)$/);
    await sources.locator("summary").click();
    await expect(sources.locator("li").first()).toBeVisible();

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
    await expect(
      page.getByText(/По базе знаний найдено следующее:|ИИ-формулировка недоступна/),
    ).toBeVisible({ timeout: 30000 });
    // Источники свёрнуты по умолчанию — раскрываем кликом по шапке.
    const sources = page.locator(".assistant-sources").first();
    await expect(sources.locator("summary")).toHaveText(/^Источники \(\d+\)$/);
    await sources.locator("summary").click();
    await expect(sources.getByText(/АВИАНДР/i).first()).toBeVisible();
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
    const sources = page.locator(".assistant-sources").first();
    await expect(sources.locator("summary")).toHaveText(/^Источники \(\d+\)$/, {
      timeout: 30000,
    });
    // Цитата карты пациента лежит в свёрнутом блоке — раскрываем.
    await sources.locator("summary").click();
    await expect(sources.getByText(/Карта пациента/)).toBeVisible();
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
  const longText = "Фрагмент про PHQ-9 и динамику шкалы. ".repeat(30);
  await openStubbedThread(page, "e2e-fragments-thread", "Деградированный ответ", [
    {
      id: "e2e-frag-q",
      role: "CLINICIAN",
      content: "Какая динамика по PHQ-9?",
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
      answer_kind: "fragments",
      llm_reason: "timeout",
      fragments: [
        { n: 1, source_title: "Клинические рекомендации: депрессия", text: longText },
        { n: 2, source_title: "Гайдлайн по тревоге", text: "Короткий второй фрагмент." },
      ],
    },
  ]);

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
  // Маркер <ol> не дублирует номер «n ·» (и не разъезжается при пропусках n).
  expect(
    await details.locator("ol").evaluate((el) => getComputedStyle(el).listStyleType),
  ).toBe("none");

  // Хвост «Источники:» срезан из текста — источники ровно один раз, из цитат,
  // и блок свёрнут: видна только шапка со счётчиком.
  await expect(page.locator(".assistant-sources summary")).toHaveText("Источники (2)");
  await expect(page.locator(".assistant-sources li").first()).toBeHidden();
  await expect(page.getByText(/Источники: 1\./)).toHaveCount(0);
});

test("карта пациента видна и в деградированном ответе (её нет во fragments)", async ({ page }) => {
  // PR-A не кладёт карту пациента во fragments (у неё нет чанка, n идут с дырой),
  // поэтому пункт ответа без своего фрагмента дорисовывается из текста.
  await openStubbedThread(page, "e2e-patient-frag", "Ответ с картой пациента", [
    { id: "e2e-patient-frag-q", role: "CLINICIAN", content: "Что с динамикой?" },
    {
      id: "e2e-patient-frag-a",
      role: "ASSISTANT",
      content:
        "По базе знаний и карте пациента найдено следующее:\n" +
        "1. — Фрагмент про PHQ-9\n" +
        "2. — КАРТА-МАРКЕР: динамика ухудшилась\n" +
        '\n\nИсточники: «Гайд» (g1), «Карта пациента: Иванов Иван» (patient:p1).',
      citations: [
        { source_title: "Гайд", guideline_id: "g1", topic: "ШКАЛЫ", score: 0.8 },
        {
          source_title: "Карта пациента: Иванов Иван",
          guideline_id: "patient:p1",
          topic: "ПАЦИЕНТ",
          score: 1,
        },
      ],
      topic: "ПАЦИЕНТ",
      answer_kind: "fragments",
      llm_reason: "empty",
      fragments: [{ n: 1, source_title: "Гайд", text: "Фрагмент про PHQ-9" }],
    },
  ]);

  const bubble = page.locator(".bubble", { hasText: "Ассистент" }).first();
  await expect(bubble.locator(".answer-degraded")).toBeVisible();
  const extra = bubble.locator(".answer-extra");
  // Карта пациента — свёрнутый блок: шапка со счётчиком, пункты после клика.
  await expect(extra.locator("summary")).toHaveText("Карта пациента (1)");
  await expect(extra.locator("li")).toBeHidden();
  await extra.locator("summary").click();
  await expect(extra).toContainText("КАРТА-МАРКЕР: динамика ухудшилась");
  // Номер пункта — тот же, что номер цитаты карты пациента.
  await expect(extra.locator("li")).toContainText("2. КАРТА-МАРКЕР");
  // Пункт, у которого есть фрагмент, во втором списке не повторяется.
  await expect(extra.locator("li")).toHaveCount(1);
  // Текст с картой не сыплется в <pre> целиком.
  await expect(bubble.locator(".assistant-answer")).toHaveCount(0);
  await expect(bubble.locator(".assistant-sources summary")).toHaveText("Источники (2)");
});

test("блоки под ответом свёрнуты: фрагменты, карта пациента и источники", async ({ page }) => {
  // Все три блока под ответом — <details> без open: до клика пунктов не видно,
  // после клика по summary они появляются, соседние блоки остаются свёрнутыми.
  await openStubbedThread(page, "e2e-collapse", "Свёрнутые блоки", [
    { id: "e2e-collapse-q", role: "CLINICIAN", content: "Что с динамикой?" },
    {
      id: "e2e-collapse-a",
      role: "ASSISTANT",
      content:
        "По базе знаний и карте пациента найдено следующее:\n" +
        "1. — Фрагмент про PHQ-9\n" +
        "2. — КАРТА-МАРКЕР: динамика ухудшилась\n" +
        '\n\nИсточники: «Гайд» (g1), «Карта пациента: Иванов Иван» (patient:p1).',
      citations: [
        { source_title: "Гайд", guideline_id: "g1", topic: "ШКАЛЫ", score: 0.8 },
        {
          source_title: "Карта пациента: Иванов Иван",
          guideline_id: "patient:p1",
          topic: "ПАЦИЕНТ",
          score: 1,
        },
      ],
      topic: "ПАЦИЕНТ",
      answer_kind: "fragments",
      llm_reason: "empty",
      fragments: [{ n: 1, source_title: "Гайд", text: "ФРАГМЕНТ-МАРКЕР: порог PHQ-9" }],
    },
  ]);

  const bubble = page.locator(".bubble", { hasText: "Ассистент" }).first();
  const fragments = bubble.locator(".answer-fragments");
  const extra = bubble.locator(".answer-extra");
  const sources = bubble.locator(".assistant-sources");
  await expect(fragments.locator("summary")).toHaveText("Фрагменты базы знаний (1)");
  await expect(extra.locator("summary")).toHaveText("Карта пациента (1)");
  await expect(sources.locator("summary")).toHaveText("Источники (2)");

  // До клика контент лежит только в закрытом details — не виден.
  await expect(fragments).toHaveJSProperty("open", false);
  await expect(extra).toHaveJSProperty("open", false);
  await expect(sources).toHaveJSProperty("open", false);
  await expect(fragments.locator(".fragment-text")).toBeHidden();
  await expect(extra.locator("li")).toBeHidden();
  await expect(sources.locator("li").first()).toBeHidden();

  // Клик по summary раскрывает свой блок и не трогает соседние.
  await fragments.locator("summary").click();
  await expect(fragments.locator(".fragment-text")).toBeVisible();
  await expect(fragments.locator(".fragment-text")).toContainText("ФРАГМЕНТ-МАРКЕР");
  await expect(extra.locator("li")).toBeHidden();
  await expect(sources.locator("li").first()).toBeHidden();

  await extra.locator("summary").click();
  await expect(extra.locator("li")).toContainText("2. КАРТА-МАРКЕР");
  await expect(sources.locator("li").first()).toBeHidden();

  await sources.locator("summary").click();
  await expect(sources.locator("li").first()).toBeVisible();
  await expect(sources).toContainText("Карта пациента: Иванов Иван");

  // Клавиатура: summary — нативная кнопка details, Enter и Space переключают.
  await sources.locator("summary").press("Enter");
  await expect(sources).toHaveJSProperty("open", false);
  await expect(sources.locator("li").first()).toBeHidden();
  await sources.locator("summary").press(" ");
  await expect(sources).toHaveJSProperty("open", true);
  await expect(sources.locator("li").first()).toBeVisible();
});

test("без цитаты пациента блок называется «Из ответа» и тоже свёрнут", async ({ page }) => {
  await openStubbedThread(page, "e2e-from-answer", "Пункт из ответа", [
    { id: "e2e-from-answer-q", role: "CLINICIAN", content: "Что с динамикой?" },
    {
      id: "e2e-from-answer-a",
      role: "ASSISTANT",
      content:
        "По базе знаний найдено следующее:\n" +
        "1. — Фрагмент про PHQ-9\n" +
        "2. — ОТВЕТ-МАРКЕР: динамика ухудшилась\n" +
        '\n\nИсточники: «Гайд» (g1).',
      citations: [{ source_title: "Гайд", guideline_id: "g1", topic: "ШКАЛЫ", score: 0.8 }],
      topic: "ШКАЛЫ",
      answer_kind: "fragments",
      llm_reason: "empty",
      fragments: [{ n: 1, source_title: "Гайд", text: "Фрагмент про PHQ-9" }],
    },
  ]);

  const extra = page
    .locator(".bubble", { hasText: "Ассистент" })
    .first()
    .locator(".answer-extra");
  // Пациента в цитатах нет (topic !== "ПАЦИЕНТ") → шапка «Из ответа (N)».
  await expect(extra.locator("summary")).toHaveText("Из ответа (1)");
  await expect(extra).toHaveJSProperty("open", false);
  await expect(extra.locator("li")).toBeHidden();
  await extra.locator("summary").click();
  await expect(extra.locator("li")).toContainText("2. ОТВЕТ-МАРКЕР");
});

test("абзац «Источники:» в середине ответа не режет текст после него", async ({ page }) => {
  // Хвост срезается только однострочный и в конце; абзац посреди ответа — часть
  // текста модели, продолжение обязано остаться.
  await openStubbedThread(page, "e2e-tail-mid", "Хвост в середине", [
    { id: "e2e-tail-mid-q", role: "CLINICIAN", content: "Вопрос" },
    {
      id: "e2e-tail-mid-a",
      role: "ASSISTANT",
      content:
        "Вступление.\n\nИсточники: см. рекомендации ниже\n\nГЛАВНЫЙ-ВЫВОД: динамика ухудшилась.",
      answer_kind: "llm",
      llm_reason: "ok",
      fragments: [],
    },
  ]);
  const answer = page.locator(".bubble", { hasText: "Ассистент" }).first().locator(".assistant-answer");
  await expect(answer).toContainText("Вступление.");
  await expect(answer).toContainText("Источники: см. рекомендации ниже");
  await expect(answer).toContainText("ГЛАВНЫЙ-ВЫВОД: динамика ухудшилась.");
});

test("контракт: незнакомый llm_reason — плашка «причина неизвестна»", async ({ page }) => {
  await openStubbedThread(page, "e2e-unknown-reason", "Незнакомый reason", [
    { id: "e2e-unkr-q", role: "CLINICIAN", content: "Вопрос" },
    {
      id: "e2e-unkr-a",
      role: "ASSISTANT",
      content: "1. — Фрагмент\n\nИсточники: «Гайд» (g1).",
      citations: [{ source_title: "Гайд", guideline_id: "g1", topic: "ШКАЛЫ", score: 0.8 }],
      answer_kind: "fragments",
      llm_reason: "rate_limited",
      fragments: [{ n: 1, source_title: "Гайд", text: "Фрагмент" }],
    },
  ]);
  const banner = page.locator(".bubble", { hasText: "Ассистент" }).first().locator(".answer-degraded");
  await expect(banner).toContainText("ИИ-формулировка недоступна");
  await expect(banner).toContainText("(причина неизвестна)");
  await expect(banner).toContainText("ниже найденные фрагменты базы знаний");
});

test("контракт: fragments без списка фрагментов — плашка и полный текст", async ({ page }) => {
  await openStubbedThread(page, "e2e-empty-fragments", "Пустой список фрагментов", [
    { id: "e2e-ef-q", role: "CLINICIAN", content: "Вопрос" },
    {
      id: "e2e-ef-a",
      role: "ASSISTANT",
      content: "По базе знаний найдено следующее:\n1. — Полный текст ответа\n\nИсточники: «Гайд» (g1).",
      citations: [{ source_title: "Гайд", guideline_id: "g1", topic: "ШКАЛЫ", score: 0.8 }],
      answer_kind: "fragments",
      llm_reason: "disabled",
      fragments: [],
    },
  ]);
  const bubble = page.locator(".bubble", { hasText: "Ассистент" }).first();
  const banner = bubble.locator(".answer-degraded");
  await expect(banner).toBeVisible();
  // Обещать фрагменты нечего — плашка нейтральная.
  await expect(banner).toContainText("ниже ответ, собранный из найденных данных");
  await expect(banner).not.toContainText("фрагменты базы знаний");
  await expect(bubble.locator(".answer-fragments")).toHaveCount(0);
  await expect(bubble.locator(".assistant-answer")).toContainText("Полный текст ответа");
  await expect(bubble.locator(".assistant-answer")).toContainText("По базе знаний найдено следующее:");
});

test("контракт: refusal — обычный текст без плашки", async ({ page }) => {
  await openStubbedThread(page, "e2e-refusal", "Отказ ассистента", [
    { id: "e2e-ref-q", role: "CLINICIAN", content: "Поставь диагноз депрессии" },
    {
      id: "e2e-ref-a",
      role: "ASSISTANT",
      content: "Я не могу поставить диагноз — это вне моих границ.",
      answer_kind: "refusal",
      llm_reason: null,
      fragments: [],
    },
  ]);

  const bubble = page.locator(".bubble", { hasText: "Ассистент" }).first();
  // Отказ — обычный текст: плашки «ИИ-формулировка недоступна» быть не должно.
  await expect(bubble.locator(".assistant-answer")).toContainText("Я не могу поставить диагноз");
  await expect(bubble.locator(".answer-degraded")).toHaveCount(0);
  await expect(bubble.locator(".answer-fragments")).toHaveCount(0);
});

test("контракт: truncated — плашка про обрыв по лимиту длины", async ({ page }) => {
  await openStubbedThread(page, "e2e-truncated", "Обрыв ответа", [
    { id: "e2e-trunc-q", role: "CLINICIAN", content: "Какой порог у PHQ-9?" },
    {
      id: "e2e-trunc-a",
      role: "ASSISTANT",
      content: "1. — Фрагмент про PHQ-9\n\nИсточники: «Гайд» (g1).",
      citations: [{ source_title: "Гайд", guideline_id: "g1", topic: "ШКАЛЫ", score: 0.8 }],
      topic: "ШКАЛЫ",
      answer_kind: "fragments",
      llm_reason: "truncated",
      fragments: [{ n: 1, source_title: "Гайд", text: "Фрагмент про PHQ-9" }],
    },
  ]);

  const banner = page.locator(".bubble", { hasText: "Ассистент" }).first().locator(".answer-degraded");
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("оборван по лимиту длины");
  // Подсказка про медленную модель — только для timeout.
  await expect(banner.locator(".answer-degraded-hint")).toHaveCount(0);
  await expect(page.locator(".answer-fragments summary")).toContainText("Фрагменты базы знаний (1)");
});

test("контракт: неизвестный answer_kind и текст из одного «Источники:»", async ({ page }) => {
  // Forward-compatibility: незнакомое значение answer_kind — обычный текст.
  await openStubbedThread(page, "e2e-unknown-kind", "Незнакомый kind", [
    { id: "e2e-unk-q", role: "CLINICIAN", content: "Вопрос" },
    {
      id: "e2e-unk-a",
      role: "ASSISTANT",
      content: "Ответ будущей версии сервера.",
      answer_kind: "summary_v2",
      llm_reason: "ok",
      fragments: [],
    },
  ]);
  const bubble = page.locator(".bubble", { hasText: "Ассистент" }).first();
  await expect(bubble.locator(".assistant-answer")).toHaveText("Ответ будущей версии сервера.");
  await expect(bubble.locator(".answer-degraded")).toHaveCount(0);
});

test("хвост «Источники:»: один бэкендовый формат, без пустого пузыря", async ({ page }) => {
  // Текст, где «Источники:» — не абзац-хвост, а часть фразы, не режется вовсе.
  await openStubbedThread(page, "e2e-tail-inline", "Хвост внутри фразы", [
    { id: "e2e-tail-inline-q", role: "CLINICIAN", content: "Вопрос" },
    {
      id: "e2e-tail-inline-a",
      role: "ASSISTANT",
      content: "Смотрите Источники: 1. «Гайд» — там подробности.",
      answer_kind: "llm",
      llm_reason: "ok",
      fragments: [],
    },
  ]);
  const bubble = page.locator(".bubble", { hasText: "Ассистент" }).first();
  await expect(bubble.locator(".assistant-answer")).toHaveText(
    "Смотрите Источники: 1. «Гайд» — там подробности.",
  );
});

test("хвост «Источники:»: ответ целиком из хвоста не даёт пустой пузырь", async ({ page }) => {
  await openStubbedThread(page, "e2e-tail-only", "Только хвост", [
    { id: "e2e-tail-only-q", role: "CLINICIAN", content: "Вопрос" },
    {
      id: "e2e-tail-only-a",
      role: "ASSISTANT",
      content: "Источники: 1. «Только хвост»",
      citations: [{ source_title: "Только хвост", guideline_id: "g1", topic: "ШКАЛЫ", score: 0.5 }],
      answer_kind: "llm",
      llm_reason: "ok",
      fragments: [],
    },
  ]);
  const bubble = page.locator(".bubble", { hasText: "Ассистент" }).first();
  await expect(bubble.locator(".assistant-answer")).toContainText("Только хвост");
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

test("ответ ИИ: хвост «Источники:» срезается, источники — один раз", async ({ page }) => {
  // answer_kind=llm: плашки нет, текст модели показывается, но служебный хвост
  // «Источники: …» не дублирует блок цитат под ответом.
  await openStubbedThread(page, "e2e-llm-thread", "Ответ ИИ", [
    { id: "e2e-llm-q", role: "CLINICIAN", content: "Какой порог тяжести у PHQ-9?" },
    {
      id: "e2e-llm-a",
      role: "ASSISTANT",
      // Хвост — ровно как его строит бэкенд: отдельным абзацем.
      content:
        "Порог тяжёлой депрессии по PHQ-9 — 20 баллов и выше [1].\n\nИсточники: «Гайд по PHQ-9» (g1).",
      citations: [{ source_title: "Гайд по PHQ-9", guideline_id: "g1", topic: "ШКАЛЫ", score: 0.9 }],
      topic: "ШКАЛЫ",
      answer_kind: "llm",
      llm_reason: "ok",
      fragments: [],
    },
  ]);

  const bubble = page.locator(".bubble", { hasText: "Ассистент" }).first();
  // Режим ИИ: плашки и списка фрагментов нет, текст — в <pre>.
  await expect(bubble.locator(".answer-degraded")).toHaveCount(0);
  await expect(bubble.locator(".answer-fragments")).toHaveCount(0);
  const text = bubble.locator(".assistant-answer");
  await expect(text).toContainText("Порог тяжёлой депрессии");
  await expect(text).not.toContainText("Источники:");
  // Блок источников один и свёрнут — текстового «Источники:» в ответе нет.
  await expect(page.locator(".assistant-sources summary")).toHaveText("Источники (1)");
  await expect(page.getByText("Источники:", { exact: true })).toHaveCount(0);
});
